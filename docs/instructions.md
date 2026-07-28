# LitGraph Operations Guide

Operational reference for running and maintaining LitGraph's infrastructure: health
checks, data ingestion jobs, and ArcadeDB server setup/maintenance on AWS. For a log of
serious bugs found during development (root cause, fix, current status), see
[`known_bugs.md`](./known_bugs.md).

## Health Checks

### ArcadeDB service

```bash
sudo systemctl status arcadedb
sudo journalctl -u arcadedb
```

### Background process management (Linux)

```bash
# Run a job in the background with tmux
tmux new -s session_name            # create a new session
ctrl b; d                           # close and detach
tmux kill-session -t session_name   # kill a session (from outside)
ctrl b; :kill-session               # kill a session (from inside)

# Print the process state. 
# - Expect R (running) or S (interruptible sleep);
# - T (stopped/suspended) indicates a problem.
ps -o state= -p <PID>

# Wider view: PID, state, and command
ps -A -o pid,stat,cmd | grep <PID>

# List jobs in the current shell; use `bg`/`fg` to resume a suspended one
jobs
```

### Crontab scheduling

```bash
crontab -l        # list current entries
crontab -e        # open it in $EDITOR to hand-edit
crontab -r        # remove it entirely (careful — no undo, no confirmation)
```

## Spinning up a new database

One ArcadeDB server can host many databases. To create a new database, give it a 
name via `ARCADEDB_DATABASE`.

**Never edit `.env` to do this.** The AWS box's `.env` is what the daily cron jobs
read to ingest real ArXiv and PubMed paper on human bio. Environment variables take 
precedence over `.env` in `pydantic-settings`, so prefix the commands instead:

```bash
export ARCADEDB_DATABASE=rice
export RUN_LOG_PATH=logs/rice_ingestion_runs.jsonl   # else `litgraph runs` mixes corpora
```

**The flip side: forgetting the prefix silently targets `lg2`.** Both database layers —
Bolt/Cypher (`neo4j_client.run_read`/`run_write`, via `_session_database()`) and HTTP/SQL
(`arcadedb_http`) — take the database name from `settings.arcadedb_database`, which falls
back to `.env`. So an unprefixed command doesn't error or warn; it just runs against
production. Verified live: the same Cypher `MATCH ()-[r:PRODUCES]->() RETURN count(r)`
returns 3,287 unprefixed (`lg2`) and 0 with `ARCADEDB_DATABASE=rice`.

`export` in the shell you work in is safer than per-command prefixes for exactly this
reason. When verifying over HTTP, putting the database in the URL path
(`/api/v1/query/rice`) is immune to the mistake; Cypher has no equivalent — it always
resolves through settings.

### 1. Create the database and core schema

```bash
uv run litgraph init-db
```

Idempotent. Creates the database if absent, then `Paper`/`Author`/`Category`/`GraphStats`
plus the unique, range, full-text, and vector indexes.

### 2. Add the biology schema

```bash
uv run python -c "from spokebio.schema_ext import ensure_schema; ensure_schema()"
```

Adds `Organism`/`Gene`/`Compound`/`Pathway`/`PubtatorChecked` and the
`MENTIONS`/`PARTICIPATES_IN`/`PRODUCES` edge types. The `scripts/` entry points call this
themselves, so this step is only for confirming schema before ingesting.

Verify both steps landed:

```bash
curl -s -u root:$ARCADEDB_PASSWORD $ARCADEDB_HTTP_URL/api/v1/databases
curl -s -u root:$ARCADEDB_PASSWORD -X POST $ARCADEDB_HTTP_URL/api/v1/query/$ARCADEDB_DATABASE \
  -H 'Content-Type: application/json' -d '{"language":"sql","command":"select from schema:types"}'
```

Expect 8 vertex types and 5 edge types.

### 3. Pick the PubMed query, and verify its hit count first

Check the query returns what you expect *before* ingesting — a wrong MeSH heading fails
silently with zero results:

```bash
curl -s -G "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi" \
  --data-urlencode "db=pubmed" --data-urlencode "retmode=json" \
  --data-urlencode "email=$NCBI_EMAIL" --data-urlencode 'term="Oryza"[Mesh]'
```

Rice is an example of that trap: `"Oryza sativa"[Mesh]` returns **0**, because MeSH
collapsed the species into the genus-level `Oryza` heading. `"Oryza"[Mesh]` returns
~51,000; adding `OR rice[Title/Abstract]` widens to ~95,000 at the cost of false
positives on "rice" as a surname.

### 4. Backload the corpus

Chunk it. Each chunk checkpoints as it goes and the next invocation resumes from the
checkpoint, so an interrupted run costs nothing:

```bash
for i in $(seq 1 12); do
  out=$(uv run litgraph backload-pubmed-api --mesh-terms '"Oryza"[Mesh]' --limit 5000 --batch-size 200)
  echo "$out"
  echo "$out" | grep -q "Backloaded 0 PubMed" && break
done
```

Embeddings are written inline during backload. If the embedding service was down for
part of the run, fill the gaps afterwards with `uv run litgraph backfill-embeddings`.

### 5. Extract entities and pathways

```bash
uv run python scripts/pubtator_mentions.py --limit 500   # Gene/Compound/Organism + MENTIONS
uv run python scripts/go_pathways.py                     # 24,129 GO biological_process Pathway nodes
```

`pubtator_mentions.py` is incremental — re-run until it reports nothing to do. Use a
small `--limit` on the first run against a shared box.

**`scripts/reactome_pathways.py` is human-only.** Reactome's current release covers 16
species and no plants at all, so running it against a plant corpus writes *human*
pathways and `PARTICIPATES_IN`/`PRODUCES` edges into that graph. Skip it for any
non-human corpus. See "Pathway edges for non-human corpora" below.

### 6. Bootstrap the stats counters

`stats overview` reads cached counters on a `GraphStats` node, which no ingestion job
populates from scratch:

```bash
uv run litgraph stats rebuild
uv run litgraph stats overview
```

### Optional: citation enrichment

Source-agnostic, so it works on any corpus, but it creates a stub `Paper` per cited work
outside the set — on `lg2` that inflated 29K real papers to 4.27M vertices. Weigh that
before running it on a shared server:

```bash
uv run litgraph enrich --limit 500
```

### Pathway edges for non-human corpora

Reactome is only relevant to human bio. To connect genes to `Pathway` nodes for a
non-human species, use GO's own annotation file (GAF) for that species instead:

```bash
uv run python scripts/go_pathways.py           # must run first — the edge upsert MATCHes Pathway
uv run python scripts/gaf_participates_in.py   # defaults to rice (ORYSJ / Oryza_sativa)
```

For another species, pass both the UniProt mnemonic that names the GAF and the NCBI
`gene_info` stem for the same organism — they use different naming schemes:

```bash
uv run python scripts/gaf_participates_in.py --species-code ARATH --organism Arabidopsis_thaliana
```

Both inputs download on first run, no license or API key needed. Measured on rice:

| Stage | Rows |
|---|---|
| `biological_process` annotations in `ORYSJ-uniprot.gaf.gz` | 37,803 |
| dropped: `NOT`-qualified (negative annotations) | 22 |
| dropped: gene not resolvable to an `ncbigene:` key | 4,885 (12.9%) |
| collapsed: duplicate (gene, pathway) pairs, best evidence code kept | 6,015 |
| **`PARTICIPATES_IN` edges written** | **26,881** |
| `Gene` nodes created on demand by those edges | 13,603 |

The 12.9% drop rate beats the 33.7% ChEBI→MeSH loss the Reactome `PRODUCES` path already
accepts. What makes it land is GAF column 3 carrying the RAP-DB locus id
(`Os01g0104100`), plus `build_gene_identifier_crosswalk` indexing gene_info's
`Symbol`/`Synonyms` alongside `LocusTag` (68.3% → 83.6% of gene products resolvable);
ambiguous synonyms like `psbA` are dropped rather than resolved to an arbitrary gene.

**This builds the reference layer, not a literature bridge.** The edges connect
`Gene`→`Pathway`, but `Paper`→`Gene` still comes from PubTator3, whose gene NER barely
fires on plant text — see the caveat below. There is no `PRODUCES` equivalent for
non-human corpora, since that path is Reactome-only.

### PubTator3 gene tagging is unusable on plant literature

Measured over the **full 51,166-paper rice corpus**: 3,826 distinct genes, i.e. 0.075
genes/paper, against `lg2`'s 0.72 — a ~10× collapse. (That rate was identical at the
600-paper sample, so it is stable, not a small-sample artifact.) The very first 100 papers
yielded exactly 1 gene, and it was `ncbigene:6654`, *human* SOS1 (a Ras GEF) — almost
certainly a misnormalization of rice SOS1 (Salt Overly Sensitive 1, an unrelated Na⁺/H⁺
antiporter). So the gene layer isn't merely sparse; its hits skew to the wrong species.

Compound and organism tagging are genuinely good, and scale well: 5,418 compounds and
5,058 organisms across 206,162 `MENTIONS` edges (~4 per paper). Top organism is taxon 4530
(*Oryza sativa*), with *Magnaporthe oryzae* (rice blast) behind it; top compounds are
Starch, Carbon, Nitrogen, Cadmium, Iron, Arsenic — all correct for rice.

The consequence shows up in the join. Of the 3,826 mentioned genes, only **107 (2.8%)**
also carry a `PARTICIPATES_IN` edge, so `Paper -MENTIONS-> Gene -PARTICIPATES_IN-> Pathway`
reaches barely a hundred genes despite all 26,881 GAF edges being loaded.

**Don't try to fix this by expanding the GAF side — it is already saturated.** Partitioning
the mentioned genes against NCBI's rice `gene_info` shows where the loss actually is:

| | Genes |
|---|---|
| mentioned in rice papers by PubTator3 | 3,826 |
| ...that are rice genes at all | **188 (4.9%)** |
| ...that are some other species | 3,638 (95.1%) |
| of those 188 rice genes: already bridged | 107 |
| of those 188 rice genes: no pathway edge yet | 81 |

So perfect GAF coverage would lift the bridge from 107 to at most **188** genes. The GAF
side is meanwhile in good shape: 13,602 genes carry pathway edges and **all 13,602 are
genuinely rice genes** (no cross-species contamination), covering 34% of the 39,965 rice
genes NCBI knows about. Re-running the loader writes nothing — it is MERGE-based and
already complete — and `ORYSJ-uniprot.gaf.gz` is the only rice GAF that GO publishes (no
Indica, no `-mod` variant).

The bottleneck is entirely on the extraction side: 95% of PubTator's gene hits on rice
literature belong to other organisms and cannot join to rice pathways at all. Only
plant-aware gene extraction (the LLM pass `plant_schema.md` proposed) moves that number —
it needs to recognise rice locus ids (`Os01g0104100`, `LOC_Os01g01010`) and community gene
names (`OsWRKY45`, `SUB1A`, `Xa21`), which is exactly what PubTator does not do.

Worth noting the reverse gap too: 13,495 of the 13,602 pathway-linked rice genes are never
mentioned by any of the 51,166 papers. The reference layer is far richer than the
literature layer can currently reach.

Note every bridging gene has a null `name`: by construction they are GAF-created nodes
(which carry no symbol) that PubTator later mentioned, and `upsert_mentions` only INSERTs
missing nodes, so a later `MENTIONS` edge never backfills the symbol. Cosmetic, but
`upsert.py`'s docstring claims MENTIONS "will fill it in later", which it does not.

### Don't run write jobs concurrently on a shared box

`scripts/pubtator_mentions.py` returned a 500 from ArcadeDB while a
`backload-pubmed-api` run was writing `Paper` vertices to the same database. Its
`MENTIONS` upsert SELECTs `Paper` by key, so it contends with an active backload. The
server and `lg2` were unaffected (`/api/v1/ready` 204 throughout), but sequence these
jobs rather than overlapping them.

## Data Ingestion

### Daily pipeline (automated)

`scripts/cron_daily_ingest.sh` runs daily to fetch new arXiv and PubMed papers, 
enrich them with citation data, then extract PubTator3 entity mentions.

```bash
./scripts/cron_daily_ingest.sh
```

**Automated**: 
```bash
# 14:30 UTC -> 9:30 EST / 10:30 EDT
30 14 * * * /home/ubuntu/litgraph/scripts/cron_daily_ingest.sh
```

Logs to `logs/cron/daily-ingest.log`. To check on a run:

```bash
tail -f logs/cron/daily-ingest.log
uv run litgraph runs --n 10
```

### One-time / occasional jobs

These populate the graph with historical or reference data. Re-run only 
when scoping changes (e.g. a new MeSH filter) or an upstream
source releases an update (e.g. a new GO/Reactome release).

#### 1. Backload PubMed papers

```bash
uv run litgraph backload-pubmed-api \
  --mesh-terms '"Molecular Biology"[Mesh] OR "Genomics"[Mesh] OR "Gene Expression Regulation"[Mesh] OR "Molecular Sequence Data"[Mesh] OR "Signal Transduction"[Mesh] OR "Systems Biology"[Mesh]'
```

Add `--limit <N>` to cap the number of records fetched in a single run.

#### 2. Backload the Kaggle arXiv dataset

Run under `tmux` since the full backload + enrichment cycle is long-running:

```bash
tmux new -s arxiv-ingest
cd ~/litgraph   # repo root on the box

mkdir -p logs
{
  echo "=== backload start: $(date) ==="
  uv run litgraph backload --file data/arxiv-metadata-oai-snapshot.json --start-date 2020-01-01

  echo "=== fetch-daily (catch up to today): $(date) ==="
  uv run litgraph fetch-daily

  echo "=== enrich (looping until nothing left): $(date) ==="
  while true; do
    out=$(uv run litgraph enrich --limit 500)
    echo "$out"
    echo "$out" | grep -q "Enriched 0 papers" && break
  done

  echo "=== done: $(date) ==="
} >> logs/arxiv_full_ingest.log 2>&1 &

# Detach with Ctrl-b, d — the job keeps running in the background.
```

To monitor progress from a separate shell (no need to reattach to `tmux`):

```bash
tail -f ~/litgraph/logs/arxiv_full_ingest.log   # live output
uv run litgraph runs --job backload
uv run litgraph runs --job enrich
uv run litgraph stats overview
```

#### 3. Extract entities via PubTator

Handled automatically by the daily pipeline above. To run standalone (e.g. to catch up
a backlog manually):

```bash
uv run scripts/pubtator_mentions.py
```

#### 4. Ingest pathway reference data (GO + Reactome)

Loads `Pathway` nodes and gene-pathway edges from Gene Ontology and Reactome. Static
reference data, not tied to paper ingestion — MERGE-based and safe to re-run, but only
needed once, or again after an upstream release:

```bash
uv run scripts/go_pathways.py
uv run scripts/reactome_pathways.py
```

#### 5. Check for a new GO/Reactome release

```bash
# GO: read the data-version line from go-basic.obo's own header. Compare against the version last ingested.
curl -sL -r 0-3000 https://purl.obolibrary.org/obo/go/go-basic.obo | grep "^data-version"

# Reactome: release number from its ContentService.
curl -s https://reactome.org/ContentService/data/database/version

# OR run the script
uv run scripts/check_pathway_releases.py
```

If either has moved on from what's already in the graph, re-ingest with
`--force-download` to bypass the local cache:

```bash
uv run scripts/go_pathways.py --force-download
uv run scripts/reactome_pathways.py --force-download
```

**Automated:** Run before the daily ingest job

```bash
7 14 * * * /home/ubuntu/litgraph/scripts/cron_pathway_release_check.sh
```

## Infrastructure

### ArcadeDB (AWS)

Connect to the instance. Host, user and key live in your local `~/.ssh/config` rather than
here, so this repo publishes nothing targetable:

```bash
# ~/.ssh/config
#   Host arcadedb-aws
#     HostName <instance-public-ip>
#     User <login-user>
#     IdentityFile ~/.ssh/<key>
ssh arcadedb-aws
cd litgraph
```

#### Fresh instance bootstrap

```bash
# System packages
sudo apt update && sudo apt upgrade

# Git access via deploy key
ssh-keygen -t ed25519 -C "your_email@example.com"
cat ~/.ssh/id_ed25519.pub   # add under Repo Settings > Deploy Keys
git clone <repo-ssh-url>

# Dependencies
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
sudo apt update && sudo apt install -y sudo lsof
```

#### Migrating to a new instance

1. Copy database data to the new instance.
2. Install ArcadeDB with the Studio UI extension.
3. Install the Bolt plugin:
   ```bash
   sudo curl -o /opt/arcadedb/lib/arcadedb-bolt-26.7.2.jar \
     https://repo1.maven.org/maven2/com/arcadedb/arcadedb-bolt/26.7.2/arcadedb-bolt-26.7.2.jar
   ```
   Remove any stale `arcadedb-bolt-*.jar` carried over from a snapshot/image of the old
   instance first — a version mismatch fails silently or throws a classloader error.
4. Update `arcadedb-server-wrapper.sh` to enable Bolt in `ARCADEDB_SETTINGS`:
   ```
   -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin \
   -Darcadedb.bolt.host=${TS_IP} \
   -Darcadedb.bolt.port=7688
   ```
   Bind to the new instance's own Tailscale IP so the port stays 7688 and no other
   config (`.env`, clients) needs to change.
5. Restart the service and verify connectivity against the new Tailscale IP:
   ```bash
   sudo systemctl restart arcadedb
   # then confirm a Bolt connection succeeds, e.g. bolt://<new-instance-ts-ip>:7688
   ```

### Embedding server (RunPod)

Same rule as above — the pod id is an access identifier, so keep it out of the repo:

```bash
# ~/.ssh/config
#   Host runpod-embed
#     HostName ssh.runpod.io
#     User <pod-id>-<pod-user>
#     IdentityFile ~/.ssh/<key>
ssh runpod-embed
```

## Troubleshooting

### Out-of-memory (ArcadeDB, AWS)

1. **Check RAM and disk headroom:**
   ```bash
   df -h   # disk space
   free -h # RAM and swap
   ```

2. **Check the JVM's configured heap size:**
   ```bash
   sudo systemctl status arcadedb --no-pager -l | head -8
   ps -ef | grep "[j]ava.*ArcadeDBServer" | grep -o "Xmx[0-9A-Za-z]*"
   ```

3. **Increase the max heap size if it's undersized for the instance:**
   ```bash
   FILE=/opt/arcadedb-26.7.2/bin/arcadedb-server-wrapper.sh
   sudo cp "$FILE" "${FILE}.bak.$(date +%Y%m%d%H%M%S)"
   sudo sed -i "s/-Xms256M -Xmx2G/-Xms256M -Xmx4G/" "$FILE"
   grep -n Xmx "$FILE"
   sudo systemctl restart arcadedb
   sleep 5
   sudo systemctl status arcadedb --no-pager -l | head -8
   ps -ef | grep "[j]ava.*ArcadeDBServer" | grep -o "Xmx[0-9A-Za-z]*"
   ```
   Note: after an instance resize, confirm this flag was updated to match the new
   available RAM — it does not change automatically.

4. **Clean up heap dumps left by the crash:**
   ```bash
   du -sh /opt/arcadedb-26.7.2/java_pid*.hprof   # check size first
   sudo rm -v /opt/arcadedb-26.7.2/java_pid*.hprof
   ```
