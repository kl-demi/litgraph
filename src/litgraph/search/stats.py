import json

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

# Not counter-backed (unlike the GraphStats singleton fields above) — a small live
# scan grouped by source family. "kaggle"/"pubmed_baseline" are the bulk-backload
# routes for the arxiv/pubmed corpora respectively, so they're folded into their
# parent family rather than shown as separate rows.
_SOURCE_BREAKDOWN = """
MATCH (p:Paper)
WHERE p.is_stub = false
RETURN CASE WHEN p.source IN ['arxiv', 'kaggle'] THEN 'arxiv'
            WHEN p.source IN ['pubmed', 'pubmed_baseline'] THEN 'pubmed'
            ELSE p.source END AS source,
       count(p) AS papers,
       count(CASE WHEN p.enriched_at IS NOT NULL THEN 1 END) AS enriched
ORDER BY papers DESC
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

# Which node types each edge type joins. Schema shape rather than data: it changes only
# when a loader starts writing a new kind of edge, but measuring it costs a full scan of
# the edge type, so it is stored on the singleton alongside the counters.
_EDGE_ENDPOINTS_SNAPSHOT = """
MATCH (g:GraphStats {id: 'singleton'})
RETURN g.edge_endpoints AS edge_endpoints
"""

_SAVE_EDGE_ENDPOINTS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.edge_endpoints = $edge_endpoints
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
    by_source = run_read(_SOURCE_BREAKDOWN)

    return {**rows[0], "top_category": top_category, "by_source": by_source}


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
    rebuild_edge_endpoints()


def _edge_type_names() -> list[str]:
    """Every edge type the database actually holds."""
    if get_settings().graph_backend == "neo4j":
        return [r["relationshipType"] for r in run_read("CALL db.relationshipTypes()")]
    rows = arcadedb_http.run_query("SELECT FROM schema:types")
    return [r["name"] for r in rows if r.get("type") == "edge"]


def _scan_edge_endpoints(name: str) -> list[list[str]]:
    """Node-type pairs one edge type joins, measured over every edge of that type."""
    if get_settings().graph_backend == "neo4j":
        rows = run_read(
            f"MATCH (a)-[:{name}]->(b) RETURN DISTINCT labels(a)[0] AS src, labels(b)[0] AS dst"
        )
    else:
        # outV()/inV() because the plain out/in projections come back null on an edge.
        # The count is discarded but not optional: ArcadeDB rejects a GROUP BY that
        # projects no aggregate.
        rows = arcadedb_http.run_query(
            f"SELECT outV().@type AS src, inV().@type AS dst, count(*) AS n "
            f"FROM `{name}` GROUP BY src, dst"
        )
    return sorted([r["src"], r["dst"]] for r in rows if r.get("src") and r.get("dst"))


def edge_endpoints() -> dict[str, list[list[str]]]:
    """Cached node-type pairs per edge type. Empty until `rebuild_stats` has run."""
    rows = run_read(_EDGE_ENDPOINTS_SNAPSHOT)
    stored = rows[0]["edge_endpoints"] if rows else None
    return json.loads(stored) if stored else {}


def rebuild_edge_endpoints() -> dict[str, list[list[str]]]:
    """Rescan every registered edge type's endpoints onto the GraphStats singleton.

    Measured rather than read off the registry, which records only what a loader
    declared: MENTIONS is registered Paper -> Gene but also reaches Compound, Organism
    and Disease. The type list comes from the database too, so an edge type written by
    a loader that was never imported here is still covered.
    """
    found = {name: _scan_edge_endpoints(name) for name in _edge_type_names()}
    run_write(_SAVE_EDGE_ENDPOINTS, edge_endpoints=json.dumps(found))
    return found


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


def type_counts() -> dict[str, dict[str, int]]:
    """Live record count for every registered node and edge type.

    Returns {"nodes": {name: count}, "edges": {name: count}}. A type registered but not
    yet created in the database counts as 0. Covers whatever is registered at call time,
    so importing `spokebio.schema_ext` first includes the biology types.
    """
    from litgraph.db.registry import registry

    def count(name: str, is_edge: bool) -> int:
        try:
            if get_settings().graph_backend == "neo4j":
                pattern = f"()-[x:{name}]->()" if is_edge else "(x:" + name + ")"
                return run_read(f"MATCH {pattern} RETURN count(x) AS c")[0]["c"]
            return arcadedb_http.run_query(f"SELECT count(*) AS c FROM `{name}`")[0]["c"]
        except Exception:
            return 0

    return {
        "nodes": {name: count(name, False) for name in registry.nodes},
        "edges": {name: count(name, True) for name in registry.edges},
    }
