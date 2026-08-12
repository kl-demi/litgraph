# litgraph

A literature knowledge graph: papers ingested from arXiv, Kaggle, and PubMed, joined
with biology entities (genes, compounds, pathways) extracted from those papers or
loaded from curated databases (GO, Reactome) — so a query can traverse from a paper
straight to the biology it's evidence for.

Backed by ArcadeDB by default (Neo4j also supported, see below). Keyword search
(full-text index), semantic search (a SPECTER2 vector index), and citation-graph
traversal, plus PubTator3-based entity extraction for the biology side.

Two packages:
- `src/litgraph/` — the source-agnostic core: schema registry, models, write path,
  search, paper ingestion, CLI.
- `src/spokebio/` — the biology extension, depending on `litgraph` as a library.

For the full design (schema, write path, ingestion, entity extraction), see
[`docs/architecture.md`](docs/architecture.md). For running it in production, see
[`docs/instructions.md`](docs/instructions.md).

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # fill in SEMANTIC_SCHOLAR_API_KEY, NCBI_EMAIL
```

## Usage

### Create a new database

Only needed once, against a fresh database:

```bash
docker compose up -d   # starts ArcadeDB, if running locally
uv run litgraph init-db
```

Add the biology schema (optional — only needed if you'll ingest GO/Reactome/PubTator3
data):

```bash
uv run python -c "from spokebio.schema_ext import ensure_schema; ensure_schema()"
```

For a new database on the shared AWS instance instead of locally, see
`docs/instructions.md`'s "Creating a new database" section.

### See what's in the graph

**Dashboard** — a Streamlit UI over the query layer: overview counts, keyword/semantic
search, citation and gene neighborhoods drawn as graphs, a raw SQL/Cypher query page,
and a schema browser with per-type record counts. A sidebar dropdown switches between
the server's databases (e.g. `lg2`, `rice`).

```bash
uv sync --extra demo   # once
streamlit run apps/dashboard.py
```

**ArcadeDB Studio** — the database's own UI, at http://localhost:2480 (user `root`,
password from `.env`) for a locally-run instance. Against the shared/production
instance, Studio is only reachable over Tailscale — talk to Arjun for access.

### CLI

If you'd rather not use the dashboard, the same queries are available directly:

```bash
uv run litgraph search keyword "diffusion models"
uv run litgraph search semantic "generative models for images"
uv run litgraph citations 1706.03762 --direction both --depth 2
uv run litgraph stats overview
```

`uv run litgraph --help` lists every command.

### Backload papers

```bash
# Backload a subset of the Kaggle arxiv-metadata-oai-snapshot.json(.gz)
# (download separately via `kaggle datasets download -d Cornell-University/arxiv`)
uv run litgraph backload --file /path/to/arxiv-metadata-oai-snapshot.json \
    --categories cs.AI,cs.CV --start-date 2023-01-01 --limit 5000

# New arXiv papers since the last run (safe to cron daily)
uv run litgraph fetch-daily --categories cs.CL,cs.LG

# PubMed, via NCBI's E-utilities
uv run litgraph backload-pubmed-api --mesh-terms '"Genomics"[Mesh]' --limit 5000
uv run litgraph fetch-daily-pubmed --mesh-terms '"Genomics"[Mesh]'

# Citation counts + citation-graph stub papers, from Semantic Scholar
uv run litgraph enrich --limit 500
```

### Biology ingestion

Standalone scripts today, not yet on the `litgraph` CLI — see `docs/architecture.md`
§7-8.

```bash
uv run scripts/go_pathways.py          # GO biological_process terms -> Pathway nodes
uv run scripts/reactome_pathways.py    # Reactome human pathways + PARTICIPATES_IN/PRODUCES
uv run scripts/pubtator_mentions.py    # PubTator3 entity mentions -> Gene/Compound/Organism
```

## Neo4j backend

If you want to migrate to the Neo4j backend: most of the codebase is backend-agnostic
Cypher, and only vector search, full-text search, and schema/index setup go through
each engine's own procedures.

```bash
docker compose -f docker-compose.neo4j.yml up -d
```

Then in `.env`, switch the backend (see the commented-out block at the bottom of
`.env.example`):

```
GRAPH_BACKEND=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<matches NEO4J_PASSWORD used to start docker-compose.neo4j.yml>
```

```bash
uv run litgraph init-db
```

Neo4j Browser is at http://localhost:7474 (user `neo4j`, password from `.env`). Every
command above works the same regardless of backend.

## Pending work & known limitations

Not tracked here to avoid drift — see [`docs/architecture.md`](docs/architecture.md)
§11 for the open work list, and [`docs/known_bugs.md`](docs/known_bugs.md) for bug
history.

## Tests

```bash
uv run pytest
```
