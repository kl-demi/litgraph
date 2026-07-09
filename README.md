# litgraph

Academic paper ingestion & search backed by ArcadeDB: keyword search, semantic (vector)
search, and citation graph traversal. Currently ingests from arXiv, with
more sources (e.g. PubMed) planned.

- **Storage**: ArcadeDB (self-hosted, Apache-2.0) by default. A vector index handles
  semantic search, a full-text index handles keyword search, and the graph itself models
  the citation network.
- **Embeddings**: `sentence-transformers/allenai-specter` (768-dim), run locally — no
  external embedding API/cost.
- **Ingestion**: historical backload from the Kaggle arXiv metadata snapshot, daily
  incremental fetch from the arXiv API, citation enrichment from Semantic Scholar.
- **Deferred**: a FastAPI query layer and cron-based daily scheduling. For now, everything
  is a CLI command and a set of plain importable functions in `litgraph.search.*`
  that a future API layer can call directly.

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # fill in SEMANTIC_SCHOLAR_API_KEY
docker compose -f docker-compose.arcadedb.yml up -d
uv run litgraph init-db
```

<!-- ArcadeDB Studio is at http://localhost:2480 (user `root`, password from `.env`). -->

## Usage

```bash
# --- Step 1: Start up container
docker compose -f docker-compose.arcadedb.yml up -d

# --- Step 2: Choose any of the following:

# Backload a subset of the Kaggle arxiv-metadata-oai-snapshot.json(.gz)
# (download separately via `kaggle datasets download -d Cornell-University/arxiv`)
uv run litgraph backload --file /path/to/arxiv-metadata-oai-snapshot.json \
    --categories cs.AI,cs.CV --start-date 2023-01-01 --limit 5000

# Enrich ingested papers with Semantic Scholar citation data
uv run litgraph enrich --limit 500

# Pull new papers submitted since the last run (safe to run daily via cron later)
uv run litgraph fetch-daily --categories cs.CL,cs.LG

# Search
uv run litgraph search keyword "diffusion models"
uv run litgraph search semantic "generative models for images"

# Citation graph
uv run litgraph citations 1706.03762 --direction both --depth 2
```

<!-- ## Alternative: Neo4j backend

Neo4j also works as a backend, toggled via `GRAPH_BACKEND`. Most of this codebase is
backend-agnostic Cypher that runs unmodified against either engine — only vector search,
full-text search, and schema/index setup differ, since those go through each engine's own
procedures/SQL (`db.index.vector.queryNodes`, `CREATE VECTOR INDEX`, etc. for Neo4j) rather
than anything in the openCypher standard both engines implement.

```bash
docker compose up -d   # starts Neo4j via docker-compose.yml
```

Then in `.env`, switch the backend (see the commented-out block at the bottom of
`.env.example`):

```
GRAPH_BACKEND=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<matches NEO4J_PASSWORD used to start docker-compose.yml>
```

```bash
uv run litgraph init-db
```

Neo4j Browser is at http://localhost:7474 (user `neo4j`, password from `.env`).
Everything else — `backload`, `enrich`, `fetch-daily`, `search keyword`, `search semantic`,
`citations` — works the same regardless of backend. -->

## Graph schema

**Nodes**
- `Paper {id, arxiv_id, s2_paper_id, title, abstract, categories, primary_category,
  published_date, updated_date, doi, journal_ref, comments, embedding, citation_count,
  reference_count, influential_citation_count, source, is_stub, fetched_at, enriched_at,
  embedded_at}` — `id` is `arxiv_id` when known, else `s2:<s2_paper_id>`. Citation targets
  outside the ingested set are written as lightweight stub nodes (`is_stub: true`) and get
  filled in automatically if that paper is later fully ingested.
- `Author {name}`, `Category {code}`

**Relationships**
- `(:Author)-[:AUTHORED]->(:Paper)`
- `(:Paper)-[:IN_CATEGORY]->(:Category)`
- `(:Paper)-[:CITES]->(:Paper)`

## Known limitations

- Author disambiguation: authors are merged by normalized name string, not a stable ID —
  two different people with the same name become one node.
- Semantic Scholar's batch endpoint caps citations/references per paper rather than
  returning the full list; landmark papers with huge citation counts are undercounted in
  the graph even though `citation_count`/`reference_count` on the node reflect the true
  totals. Upgrading to the paginated `/paper/{id}/citations` and `/paper/{id}/references`
  endpoints is the natural next step if exhaustive edges are needed.
- `enrich` only processes papers that have never been enriched (`enriched_at IS NULL`);
  there's no re-enrichment of stale citation counts yet.

## Tests

```bash
uv run pytest
```
