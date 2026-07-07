# arxiv-graphdb

arXiv paper ingestion & search backed by Neo4j: keyword search, semantic (vector) search,
and citation graph traversal in one database.

- **Storage**: Neo4j. A native vector index handles semantic search, a full-text index
  handles keyword search, and the graph itself models the citation network — no separate
  vector DB or search engine.
- **Embeddings**: `sentence-transformers/allenai-specter` (768-dim), run locally — no
  external embedding API/cost.
- **Ingestion**: historical backload from the Kaggle arXiv metadata snapshot, daily
  incremental fetch from the arXiv API, citation enrichment from Semantic Scholar.
- **Deferred**: a FastAPI query layer and cron-based daily scheduling. For now, everything
  is a CLI command and a set of plain importable functions in `arxiv_graphdb.search.*`
  that a future API layer can call directly.

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # fill in NEO4J_PASSWORD / SEMANTIC_SCHOLAR_API_KEY
docker compose up -d
uv run arxiv-graphdb init-db
```

Neo4j Browser is at http://localhost:7474 (user `neo4j`, password from `.env`).

## Usage

```bash
# Backload a subset of the Kaggle arxiv-metadata-oai-snapshot.json(.gz)
# (download separately via `kaggle datasets download -d Cornell-University/arxiv`)
uv run arxiv-graphdb backload --file /path/to/arxiv-metadata-oai-snapshot.json \
    --categories cs.CL,cs.LG --start-date 2023-01-01 --limit 5000

# Enrich ingested papers with Semantic Scholar citation data
uv run arxiv-graphdb enrich --limit 500

# Pull new papers submitted since the last run (safe to run daily via cron later)
uv run arxiv-graphdb fetch-daily --categories cs.CL,cs.LG

# Search
uv run arxiv-graphdb search keyword "diffusion models"
uv run arxiv-graphdb search semantic "generative models for images"

# Citation graph
uv run arxiv-graphdb citations 1706.03762 --direction both --depth 2
```

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
