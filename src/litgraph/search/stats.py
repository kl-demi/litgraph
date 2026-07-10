from litgraph.db.neo4j_client import run_read

_PAPER_COUNT = """
MATCH (p:Paper)
WHERE p.is_stub = false
RETURN count(p) AS paper_count
"""

# Ingested papers only (is_stub = false) — citation-graph stub placeholders (papers
# referenced/citing but never ingested themselves) are counted separately, since they
# have no title/abstract/authors/etc. and would otherwise inflate "papers".
_OVERVIEW_COUNTS = """
MATCH (p:Paper)
WHERE p.is_stub = false
RETURN count(p) AS papers,
       count(CASE WHEN p.citation_count IS NOT NULL THEN 1 END) AS enriched,
       count(CASE WHEN p.embedding IS NOT NULL THEN 1 END) AS embedded,
       min(p.published_date) AS earliest_published,
       max(p.published_date) AS latest_published
"""

_STUB_COUNT = """
MATCH (p:Paper)
WHERE p.is_stub = true
RETURN count(p) AS stubs
"""

_OVERVIEW_NODE_COUNTS = """
MATCH (a:Author)
WITH count(a) AS authors
MATCH (c:Category)
RETURN authors, count(c) AS categories
"""

_OVERVIEW_EDGE_COUNTS = """
OPTIONAL MATCH ()-[cites:CITES]->()
WITH count(cites) AS citation_edges
OPTIONAL MATCH ()-[authored:AUTHORED]->()
WITH citation_edges, count(authored) AS authored_edges
OPTIONAL MATCH ()-[in_cat:IN_CATEGORY]->()
RETURN citation_edges, authored_edges, count(in_cat) AS category_edges
"""

_TOP_CATEGORY = """
MATCH (p:Paper)-[:IN_CATEGORY]->(c:Category)
RETURN c.code AS code, count(p) AS paper_count
ORDER BY paper_count DESC
LIMIT 1
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
    counts = run_read(_OVERVIEW_COUNTS)[0]
    node_counts = run_read(_OVERVIEW_NODE_COUNTS)[0]
    edge_counts = run_read(_OVERVIEW_EDGE_COUNTS)[0]
    stubs = run_read(_STUB_COUNT)[0]["stubs"]
    top_category_rows = run_read(_TOP_CATEGORY)
    top_category = top_category_rows[0] if top_category_rows else None

    return {
        **counts,
        **node_counts,
        **edge_counts,
        "stubs": stubs,
        "top_category": top_category,
    }


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
