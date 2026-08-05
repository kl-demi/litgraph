# LitGraph Operations Guide

Operational guide for running and maintaining LitGraph's infrastructure: health
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

## Common Queries
```sql
-- Direct neighbors
MATCH (d:Drug {name: 'Baricitinib'})-[r]-(n)
RETURN d, r, n

-- Shortest path between two arbitrary nodes
MATCH p = shortestPath(
  (d:Drug {name:'Baricitinib'})-[*..4]-(dis:Disease {name:'COVID-19'})
)
RETURN p

-- Mechanism-pattern
MATCH (drug:Drug)-[:INHIBITS]->(protein:Protein)
      -[:PARTICIPATES_IN]->(pathway:Pathway)
      -[:DYSREGULATED_IN]->(disease:Disease {name:'COVID-19'})
WHERE drug.approved = true
RETURN drug.name, protein.name, pathway.name

MATCH (p:Paper)-[:MENTIONS]->(g:Gene)-[:ASSOCIATED_WITH]->(t:Trait {name:'drought tolerance'})
MATCH (g)-[:PARTICIPATES_IN]->(w:Pathway)
RETURN t.name, g.name, w.name, count(DISTINCT p) AS papers ORDER BY papers DESC
```


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

## Creating a new database

To create a new database, give it a 
name via `ARCADEDB_DATABASE` as an environment variable. **Do not edit `.env`** as daily cron jobs on AWS read from it. 

```bash
export ARCADEDB_DATABASE=rice
export RUN_LOG_PATH=logs/rice_ingestion_runs.jsonl   # else `litgraph runs` mixes corpora
```

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

Adds `Organism`/`Gene`/`Compound`/`Pathway`/`Trait`/`PubtatorChecked` and the
`MENTIONS`/`PARTICIPATES_IN`/`PRODUCES`/`ASSOCIATED_WITH` edge types.

Verify both steps landed:

```bash
curl -s -u root:$ARCADEDB_PASSWORD $ARCADEDB_HTTP_URL/api/v1/databases
curl -s -u root:$ARCADEDB_PASSWORD -X POST $ARCADEDB_HTTP_URL/api/v1/query/$ARCADEDB_DATABASE \
  -H 'Content-Type: application/json' -d '{"language":"sql","command":"select from schema:types"}'
```

Expect 9 vertex types and 6 edge types.

### 3. Pick the PubMed query, and verify its hit count first

Check the query returns what you expect *before* ingesting — a wrong MeSH heading fails
silently with zero results:

```bash
curl -s -G "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi" \
  --data-urlencode "db=pubmed" --data-urlencode "retmode=json" \
  --data-urlencode "email=$NCBI_EMAIL" --data-urlencode 'term="Oryza"[Mesh]'
```

### 4. Backload the corpus

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

### 6. Give key-only genes a readable name

Run **last**, after every extraction pass. The GAF and Oryzabase loaders create `Gene`
nodes key-only (their sources are keyed on locus ids and carry no symbol), so queries come
back `gene: null` even though the graph knows the gene:

```bash
uv run python scripts/backfill_gene_names.py --organism Oryza_sativa
```

Safe to re-run and idempotent. Three naming sources, in priority order:

| Tier | Source | What it gives |
|---|---|---|
| 1 | Oryzabase `CGSNL Gene Symbol` | the curated symbol (`SD1`, `GHD7`, `BPH9`) |
| 2 | gene_info `Symbol` | a real symbol, for rice mostly organellar (`psbA`, `matK`) |
| 3 | gene_info locus id | RAP-DB (`Os01g0970700`), else MSU/TIGR (`LOC_Os01g73880`) |
| — | NCBI's `LOC<GeneID>` | **never written** |

**Why Oryzabase outranks NCBI here.** Rice's nomenclature authority is Oryzabase's CGSNL,
and it does not feed NCBI — `Symbol_from_nomenclature_authority` is empty on all 39,965
rice gene_info rows, versus 96.9% populated for Arabidopsis (TAIR) and 94.4% for human
(HGNC), counting protein-coding genes. So NCBI's `Symbol` is `LOC<GeneID>` for 96% of rice
genes while Oryzabase carries a real symbol on all ~22K of its rows. This is not an
understudied-organism effect; it is one authority never having been wired into NCBI's
pipeline.

`LOC<GeneID>` is never written because it restates the key and would hide which genes
genuinely lack a symbol.

**Both RAP-DB and MSU/TIGR are needed.** 22,459 rice gene_info rows carry a RAP id and
3,464 an MSU id, but only 419 carry both — they are largely disjoint, so dropping MSU
strands its genes. Note MSU's `LOC_` prefix is unrelated to NCBI's `LOC<GeneID>`; both
patterns are anchored (`is_locus_id`) because a `startswith("LOC")` test would discard
every MSU id.

**It upgrades as well as fills**, which matters because `backfill_gene_names` is null-only:
a gene named by tier 3 on an earlier run would display a bare `Os08g0238500` forever. So a
name that is still merely *provisional* — a locus id, or a `LOC<GeneID>` an extractor
relayed — is replaced once a curated symbol is available. Never a curator- or
extractor-assigned symbol, and the check runs against what is actually stored
(`upsert.read_gene_names` → `is_provisional_name` → `upsert.upgrade_gene_names`, the one
write in this file with no `WHERE` guard).

Result on rice, from 11,672 nameless genes:

| Display name | Genes |
|---|---|
| real symbol | 12,863 |
| RAP-DB locus id | 5,875 |
| MSU/TIGR locus id | 442 |
| `LOC<GeneID>` (extractor-relayed, no curated symbol exists) | 341 |
| still null | 57 |

**A caveat on coverage, not naming.** `SD1`, `GHD7` and `SUB1A` — among the most-studied
rice genes there are — cannot be named because they are not `Gene` nodes at all: their
RAP ids have no `ncbigene:` mapping in gene_info, so the `ncbigene:`-keyed schema cannot
represent them. That is a crosswalk-coverage limit, not a naming one, and no naming tier
fixes it.

`pubtator_mentions.py` also fills names now, for genes a pathway loader created bare before
a paper named them — `_upsert_entities_sql` writes `name` on INSERT only, so those stayed
null forever. Note this is per-run: papers already in `PubtatorChecked` are never
re-queried, so genes stranded by an earlier run need their `PubtatorChecked` rows deleted
before a re-run can name them (bookkeeping nodes only — every write on that path is
insert-if-missing, so re-running is safe).

### 7. Bootstrap the stats counters

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

To connect genes to `Pathway` nodes for a
non-human species, use GO's own annotation file (GAF) for that species:

```bash
uv run python scripts/go_pathways.py           # must run first — the edge upsert MATCHes Pathway
uv run python scripts/gaf_participates_in.py   # defaults to rice (ORYSJ / Oryza_sativa)
```

For another species, pass both the UniProt mnemonic that names the GAF and the NCBI
`gene_info` stem for the same organism — they use different naming schemes:

```bash
uv run python scripts/gaf_participates_in.py --species-code ARATH --organism Arabidopsis_thaliana
```

**This builds the reference layer, not a literature bridge.** The edges connect
`Gene`→`Pathway`, but `Paper`→`Gene` still comes from PubTator3, whose gene NER barely
fires on plant text. There is no `PRODUCES` equivalent for
non-human corpora, since that path is Reactome-only.

### Trait edges for rice (trait-centric queries)

Loads the Trait Ontology as `Trait` nodes, then Oryzabase's curated rice gene-trait
annotations as `Gene`→`Trait` `ASSOCIATED_WITH` edges. Run in this order — the edge
upsert MATCHes `Trait` nodes rather than creating them, so without step 1 step 2 writes
nothing:

```bash
uv run python scripts/to_traits.py          # 1,587 non-obsolete TO terms -> Trait nodes
uv run python scripts/oryzabase_traits.py   # ~33.5K Gene -> Trait edges (rice only)
```

Both are MERGE-based and safe to re-run. Re-run after a new TO release, or when
Oryzabase refreshes its export.

Rice-specific by construction: Oryzabase is a rice database and gene resolution depends
on NCBI's `Oryza_sativa` gene_info file, so there's no `--species-code` switch the way
`gaf_participates_in.py` has one.

Expected drops on a clean run, none of them errors:

| Drop | Count | Why |
|---|---|---|
| Gene unresolvable | ~1,558 of 8,503 rows (18%) | Bracketed classical mutants (`[CMS-54257]`) with no locus id and no molecular identity |
| Obsolete TO term | ~646 edges (1.9%) | Oryzabase annotates against TO ids that TO has since obsoleted; reported, not silent |
| Duplicate pair | ~114 | Same gene-trait pair on multiple rows |

Two parsing gotchas, both handled in `ingest/oryzabase.py` but worth knowing if you touch
it: the export declares `charset=Windows-31J` but the bytes are **UTF-8 with a BOM**
(honouring the declared charset raises `UnicodeDecodeError` partway through), and rice
RAP-DB ids live in gene_info's **`Other_designations`** column, not `LocusTag` — which is
why this path uses `build_locus_identifier_crosswalk` rather than the GAF path's
`build_gene_identifier_crosswalk` (20.2% vs 81.7% resolution).

### Rice gene mentions from paper text (the Paper->Gene bottleneck)

PubTator3's gene NER barely fires on rice: 7.4% of papers, and only 4.9% of the genes it
names are rice genes (the rest are human/mouse orthologs, or Arabidopsis). This adds a
dictionary pass built from Oryzabase's own symbols and synonyms:

```bash
uv run python scripts/gazetteer_mentions.py --dry-run   # ALWAYS run this first
uv run python scripts/gazetteer_mentions.py
```

Additive to PubTator, never a replacement: edges are only created where none exists, and
new ones are stamped `source="oryzabase-gazetteer"` so the two extractors stay separable
and either can be reverted alone. (The ~206K MENTIONS edges predating this carry no
`source` and are all PubTator3's.) Re-running is idempotent, and also backfills a readable
`name` onto Gene nodes the GAF/Oryzabase loaders created key-only.

**Why `--dry-run` first.** A gazetteer's precision depends entirely on how much its
vocabulary overlaps ordinary English, and the report's most-matched-forms list is where
that shows up. Real failures caught this way:

| Form | Genuine rice gene? | What it actually matched |
|---|---|---|
| `SALT` | yes | the word "salt" -- 979 hits in a 6,000-paper sample |
| `ML-1` | yes | the unit "µg **mL-1**" -- 154 hits |
| `WD40` | no | a protein *domain* |
| `NPR1`, `BRI1` | yes | usually the *Arabidopsis* gene; rice writes `OsNPR1`/`OsBRI1` |

So only **unambiguous** forms are admitted, and by default only two classes: those safe by
construction (a RAP/MSU locus id, or an `Os`-prefixed symbol of 5+ chars -- 55% of all
matches), plus an explicit allowlist of hand-verified rice symbols (`HD3A`, `XA21`,
`GHD7`, `EHD1`, `RFT1`, `SLR1`, `SUB1`, `DEP1`, `BADH2`, `NAL1`, `IPA1`, ...). `GHD7` and
`WD40` are structurally identical -- letters plus digits -- so nothing but the biology
separates them, which is why that list is explicit rather than a rule.

`--include-unaudited` adds a permissive tier (any 4+ char letters+digits symbol, minus
units and known rejects). It reaches 22.3% of papers instead of 15.2%, but ~36% of its
matches are forms nobody has verified. It exists to generate candidates for a later LLM
disambiguation pass, not for routine loading.

Measured on the 51,166-paper corpus:

| | papers with a gene | mentions | distinct rice genes |
|---|---|---|---|
| PubTator3 alone | 3,791 (7.4%) | 479 | 188 |
| **+ gazetteer (default)** | **10,639** | **+16,003** | **7,928** |

Effect on the trait-centric query: papers supporting
`Paper->Gene->{Trait, Pathway}` went from **104 to 6,498** (530 traits, 1,231 pathways).

Trait-centric query this enables — the gene is the hub, so `Trait` and `Pathway` both
hang off it:

```sql
MATCH (p:Paper)-[:MENTIONS]->(g:Gene)-[:ASSOCIATED_WITH]->(t:Trait {name:'drought tolerance'})
MATCH (g)-[:PARTICIPATES_IN]->(w:Pathway)
RETURN t.name, g.name, w.name, count(DISTINCT p) AS papers
ORDER BY papers DESC
```

The `Paper`→`Gene` hop is still the bottleneck (PubTator finds a rice gene in only 7.4%
of papers); the reference layer below it is dense (4,710 genes carry both a trait and a
pathway edge).

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
