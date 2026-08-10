# LitGraph Operations Guide

Commands for running and maintaining LitGraph. For bug history and troubleshooting
steps, see [`known_bugs.md`](./known_bugs.md).

## Health Checks

### ArcadeDB service

```bash
sudo systemctl status arcadedb --no-pager -l | head -8
sudo journalctl -u arcadedb

```

### Box' memory

```bash
df -h   # disk
free -h # RAM/swap
dmesg -T | grep -i oom
```

### Background process management (Linux)

```bash
# Run a job in the background with tmux
tmux new -s session_name            # create a new session
ctrl b; d                           # close and detach
tmux kill-session -t session_name   # kill a session (from outside)
ctrl b; :kill-session               # kill a session (from inside)

ps -o state= -p <PID>                  # R/S = fine, T = stopped/suspended
ps -A -o pid,stat,cmd | grep <PID>      # PID, state, command
jobs                                    # jobs in the current shell; bg/fg to resume
```

### Crontab

```bash
crontab -l        # list current entries
crontab -e        # edit in $EDITOR
crontab -r        # remove entirely (no undo, no confirmation)
```

## Common Queries

```sql
-- Genes a paper mentions
MATCH (p:Paper {id: 'pmid:12345678'})-[:MENTIONS]->(g:Gene)
RETURN g.gene_id, g.name

-- Pathways a gene participates in
MATCH (g:Gene {gene_id: 'ncbigene:7157'})-[:PARTICIPATES_IN]->(pw:Pathway)
RETURN pw.pathway_id, pw.name, pw.source_db

-- Compounds a pathway produces
MATCH (pw:Pathway)-[:PRODUCES]->(c:Compound)
RETURN pw.name, c.name LIMIT 20

-- Papers supporting a gene -> pathway link
MATCH (p:Paper)-[:MENTIONS]->(g:Gene {gene_id: 'ncbigene:7157'})
MATCH (g)-[:PARTICIPATES_IN]->(pw:Pathway)
RETURN p.title, pw.name
```

## Data Ingestion

### Daily pipeline (automated)

```bash
./scripts/cron_daily_ingest.sh
```

Fetches new arXiv/PubMed papers, enriches citations, extracts PubTator3 mentions.

```bash
# crontab: 14:30 UTC -> 9:30 EST / 10:30 EDT
30 14 * * * /home/ubuntu/litgraph/scripts/cron_daily_ingest.sh
```

```bash
tail -f logs/cron/daily-ingest.log
uv run litgraph runs --n 10
```

### One-time / occasional jobs

Re-run only on a scoping change or an upstream release.

**1. Backload PubMed papers**

```bash
uv run litgraph backload-pubmed-api \
  --mesh-terms '"Molecular Biology"[Mesh] OR "Genomics"[Mesh] OR "Gene Expression Regulation"[Mesh] OR "Molecular Sequence Data"[Mesh] OR "Signal Transduction"[Mesh] OR "Systems Biology"[Mesh]'
```

`--limit <N>` caps the number of records per run.

**2. Backload the Kaggle arXiv dataset**

```bash
tmux new -s arxiv-ingest
cd ~/litgraph
mkdir -p logs
{
  uv run litgraph backload --file data/arxiv-metadata-oai-snapshot.json --start-date 2020-01-01
  uv run litgraph fetch-daily
  while true; do
    out=$(uv run litgraph enrich --limit 500)
    echo "$out" | grep -q "Enriched 0 papers" && break
  done
} >> logs/arxiv_full_ingest.log 2>&1 &
# Ctrl-b, d to detach
```

```bash
tail -f ~/litgraph/logs/arxiv_full_ingest.log
uv run litgraph runs --job backload
uv run litgraph runs --job enrich
uv run litgraph stats overview
```

**3. Extract entities via PubTator**

Runs automatically in the daily pipeline. To catch up a backlog manually:

```bash
uv run scripts/pubtator_mentions.py
```

**4. Ingest pathway reference data (GO + Reactome)**

MERGE-based, safe to re-run, only needed once or after an upstream release:

```bash
uv run scripts/go_pathways.py
uv run scripts/reactome_pathways.py
```

**5. Check for a new GO/Reactome release**

```bash
curl -sL -r 0-3000 https://purl.obolibrary.org/obo/go/go-basic.obo | grep "^data-version"
curl -s https://reactome.org/ContentService/data/database/version
# or:
uv run scripts/check_pathway_releases.py
```

If either moved on, re-ingest with `--force-download`:

```bash
uv run scripts/go_pathways.py --force-download
uv run scripts/reactome_pathways.py --force-download
```

```bash
# crontab, run before the daily ingest job
7 14 * * * /home/ubuntu/litgraph/scripts/cron_pathway_release_check.sh
```

## Infrastructure

### ArcadeDB (AWS)

Host/user/key live in `~/.ssh/config`, not here:

```bash
# ~/.ssh/config
#   Host arcadedb-aws
#     HostName <instance-public-ip>
#     User <login-user>
#     IdentityFile ~/.ssh/<key>
ssh arcadedb-aws
cd litgraph
```

**Fresh instance bootstrap:**

```bash
sudo apt update && sudo apt upgrade

ssh-keygen -t ed25519 -C "your_email@example.com"
cat ~/.ssh/id_ed25519.pub   # add under Repo Settings > Deploy Keys
git clone <repo-ssh-url>

curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt update && sudo apt install -y sudo lsof
```

**Migrating to a new instance:**

```bash
# 1. Copy database data to the new instance.
# 2. Install ArcadeDB with the Studio UI extension.
# 3. Install the Bolt plugin (remove any stale arcadedb-bolt-*.jar first):
sudo curl -o /opt/arcadedb/lib/arcadedb-bolt-26.7.2.jar \
  https://repo1.maven.org/maven2/com/arcadedb/arcadedb-bolt/26.7.2/arcadedb-bolt-26.7.2.jar
```

```
# 4. In arcadedb-server-wrapper.sh, ARCADEDB_SETTINGS:
-Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin \
-Darcadedb.bolt.host=${TS_IP} \
-Darcadedb.bolt.port=7688
```

```bash
# 5. Restart and verify
sudo systemctl restart arcadedb
# confirm a Bolt connection succeeds, e.g. bolt://<new-instance-ts-ip>:7688
```

### Embedding server (RunPod)

```bash
# ~/.ssh/config
#   Host runpod-embed
#     HostName ssh.runpod.io
#     User <pod-id>-<pod-user>
#     IdentityFile ~/.ssh/<key>
ssh runpod-embed
```

## Creating a new database

Name it via `ARCADEDB_DATABASE`. **Do not edit `.env`** — cron reads it.

```bash
export ARCADEDB_DATABASE=<name>
export RUN_LOG_PATH=logs/<name>_ingestion_runs.jsonl   # else `litgraph runs` mixes corpora
```

**1. Create the database and core schema**

```bash
uv run litgraph init-db
```

**2. Add the biology schema**

```bash
uv run python -c "from spokebio.schema_ext import ensure_schema; ensure_schema()"
```

Verify:

```bash
curl -s -u root:$ARCADEDB_PASSWORD $ARCADEDB_HTTP_URL/api/v1/databases
curl -s -u root:$ARCADEDB_PASSWORD -X POST $ARCADEDB_HTTP_URL/api/v1/query/$ARCADEDB_DATABASE \
  -H 'Content-Type: application/json' -d '{"language":"sql","command":"select from schema:types"}'
```

Expect 9 vertex types and 6 edge types.

**3. Pick the PubMed query, check its hit count first**

```bash
curl -s -G "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi" \
  --data-urlencode "db=pubmed" --data-urlencode "retmode=json" \
  --data-urlencode "email=$NCBI_EMAIL" --data-urlencode 'term="Oryza"[Mesh]'
```

**4. Backload the corpus**

```bash
for i in $(seq 1 12); do
  out=$(uv run litgraph backload-pubmed-api --mesh-terms '"Oryza"[Mesh]' --limit 5000 --batch-size 200)
  echo "$out" | grep -q "Backloaded 0 PubMed" && break
done
```

If the embedding service was down for part of the run: `uv run litgraph backfill-embeddings`.

**5. Extract entities and pathways**

```bash
uv run python scripts/pubtator_mentions.py --limit 500
uv run python scripts/go_pathways.py
uv run python scripts/reactome_pathways.py
```

**6. Bootstrap the stats counters**

```bash
uv run litgraph stats rebuild
uv run litgraph stats overview
```

**Optional: citation enrichment.** Creates a stub `Paper` per cited work outside the
corpus — this can dwarf the real paper count (seen: 29K real papers -> 4.27M vertices).
Weigh that before running it on a shared server.

```bash
uv run litgraph enrich --limit 500
```
