from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_read, run_write

_PAPER_COUNT = """
MATCH (p:Paper)
WHERE p.is_stub = false
RETURN count(p) AS paper_count
"""

# `overview()` reads pre-computed counters off a GraphStats singleton, kept in sync
# incrementally by every write in graph/upsert.py, instead of full-scanning the graph
# on every call. `rebuild_stats()` recomputes those counters from scratch (the
# full-scan queries below) — used to bootstrap the singleton on first use, or to
# correct drift if it's ever manually invoked via `litgraph stats rebuild`.
_GRAPHSTATS_SNAPSHOT = """
MATCH (g:GraphStats {id: 'singleton'})
RETURN g.papers AS papers,
       g.stubs AS stubs,
       g.enriched AS enriched,
       g.embedded AS embedded,
       g.authors AS authors,
       g.categories AS categories,
       g.authored_edges AS authored_edges,
       g.category_edges AS category_edges,
       g.citation_edges AS citation_edges,
       g.earliest_published AS earliest_published,
       g.latest_published AS latest_published
"""

# Category count is bounded (arXiv/MeSH categories, not papers), so this scan stays
# cheap even though it isn't counter-backed.
_TOP_CATEGORY = """
MATCH (c:Category)
WHERE c.paper_count IS NOT NULL
RETURN c.code AS code, c.paper_count AS paper_count
ORDER BY paper_count DESC
LIMIT 1
"""

# Ingested papers only (is_stub = false) — citation-graph stub placeholders (papers
# referenced/citing but never ingested themselves) are counted separately, since they
# have no title/abstract/authors/etc. and would otherwise inflate "papers".
_REBUILD_COUNTS = """
MATCH (p:Paper)
WHERE p.is_stub = false
RETURN count(p) AS papers,
       count(CASE WHEN p.citation_count IS NOT NULL THEN 1 END) AS enriched,
       count(CASE WHEN p.embedding IS NOT NULL THEN 1 END) AS embedded,
       min(p.published_date) AS earliest_published,
       max(p.published_date) AS latest_published
"""

_REBUILD_STUB_COUNT = """
MATCH (p:Paper)
WHERE p.is_stub = true
RETURN count(p) AS stubs
"""

_REBUILD_NODE_COUNTS = """
MATCH (a:Author)
WITH count(a) AS authors
MATCH (c:Category)
RETURN authors, count(c) AS categories
"""

_REBUILD_EDGE_COUNTS = """
OPTIONAL MATCH ()-[cites:CITES]->()
WITH count(cites) AS citation_edges
OPTIONAL MATCH ()-[authored:AUTHORED]->()
WITH citation_edges, count(authored) AS authored_edges
OPTIONAL MATCH ()-[in_cat:IN_CATEGORY]->()
RETURN citation_edges, authored_edges, count(in_cat) AS category_edges
"""


def _rebuild_edge_counts() -> dict:
    """Counting an entire edge type with no anchor node (`()-[:TYPE]->()`) is
    reliably 100x+ slower over ArcadeDB's Cypher/Bolt layer than the identical count
    via its native SQL engine (observed: ~9s vs ~0.1s per type on this deployment) —
    so on ArcadeDB, go straight through the SQL/HTTP API instead. Neo4j doesn't have
    this issue, so it keeps using the plain Cypher query."""
    if get_settings().graph_backend == "neo4j":
        return run_read(_REBUILD_EDGE_COUNTS)[0]
    return {
        "citation_edges": arcadedb_http.run_query("SELECT count(*) AS c FROM CITES")[0]["c"],
        "authored_edges": arcadedb_http.run_query("SELECT count(*) AS c FROM AUTHORED")[0]["c"],
        "category_edges": arcadedb_http.run_query("SELECT count(*) AS c FROM IN_CATEGORY")[0]["c"],
    }

_REBUILD_CATEGORY_PAPER_COUNTS = """
MATCH (c:Category)
OPTIONAL MATCH (p:Paper)-[:IN_CATEGORY]->(c)
WITH c, count(p) AS paper_count
SET c.paper_count = paper_count
"""

_REBUILD_GRAPHSTATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.papers = $papers,
    g.stubs = $stubs,
    g.enriched = $enriched,
    g.embedded = $embedded,
    g.authors = $authors,
    g.categories = $categories,
    g.authored_edges = $authored_edges,
    g.category_edges = $category_edges,
    g.citation_edges = $citation_edges,
    g.earliest_published = $earliest_published,
    g.latest_published = $latest_published
"""

_LATEST_PAPERS = """
MATCH (p:Paper)
WHERE p.published_date IS NOT NULL
OPTIONAL MATCH (a:Author)-[:AUTHORED]->(p)
WITH p, collect(a.name) AS authors
RETURN p.arxiv_id AS arxiv_id, p.title AS title, p.published_date AS published_date, authors
ORDER BY p.published_date DESC
LIMIT $limit
"""

_OLDEST_PAPERS = """
MATCH (p:Paper)
WHERE p.published_date IS NOT NULL
OPTIONAL MATCH (a:Author)-[:AUTHORED]->(p)
WITH p, collect(a.name) AS authors
RETURN p.arxiv_id AS arxiv_id, p.title AS title, p.published_date AS published_date, authors
ORDER BY p.published_date ASC
LIMIT $limit
"""

_TOP_AUTHORS = """
MATCH (a:Author)-[:AUTHORED]->(p:Paper)
RETURN a.name AS name, count(p) AS paper_count
ORDER BY paper_count DESC
LIMIT $limit
"""


def paper_count() -> int:
    """Total number of ingested (non-stub) papers in the graph."""
    return run_read(_PAPER_COUNT)[0]["paper_count"]


def overview() -> dict:
    """A snapshot of what's in the graph: counts, enrichment coverage, date range."""
    rows = run_read(_GRAPHSTATS_SNAPSHOT)
    if not rows:
        rebuild_stats()
        rows = run_read(_GRAPHSTATS_SNAPSHOT)

    top_category_rows = run_read(_TOP_CATEGORY)
    top_category = top_category_rows[0] if top_category_rows else None

    return {**rows[0], "top_category": top_category}


def rebuild_stats() -> None:
    """Recompute the GraphStats singleton from scratch via full graph scans.

    Slow (full scans of Paper/Author/Category/edges) — only needed to bootstrap the
    singleton the first time, or to correct drift if it's ever suspected (e.g. a crash
    mid-batch between an upsert and its stats-delta write).
    """
    counts = run_read(_REBUILD_COUNTS)[0]
    node_counts = run_read(_REBUILD_NODE_COUNTS)[0]
    edge_counts = _rebuild_edge_counts()
    stubs = run_read(_REBUILD_STUB_COUNT)[0]["stubs"]

    run_write(_REBUILD_CATEGORY_PAPER_COUNTS)
    run_write(
        _REBUILD_GRAPHSTATS,
        stubs=stubs,
        **counts,
        **node_counts,
        **edge_counts,
    )


def latest_papers(limit: int = 10) -> list[dict]:
    """The most recently published papers, with their authors."""
    rows = run_read(_LATEST_PAPERS, limit=limit)
    for row in rows:
        row["authors"] = ", ".join(a for a in row["authors"] if a)
    return rows

def oldest_papers(limit: int = 10) -> list[dict]:
    """The least recently published papers, with their authors."""
    rows = run_read(_OLDEST_PAPERS, limit=limit)
    for row in rows:
        row["authors"] = ", ".join(a for a in row["authors"] if a)
    return rows


def top_authors(limit: int = 10) -> list[dict]:
    """Authors with the most papers, by AUTHORED edge count."""
    return run_read(_TOP_AUTHORS, limit=limit)
