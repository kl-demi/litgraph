from litgraph.db.neo4j_client import run_read

_PAPER_COUNT = """
MATCH (p:Paper)
RETURN count(p) AS paper_count
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

_TOP_AUTHORS = """
MATCH (a:Author)-[:AUTHORED]->(p:Paper)
RETURN a.name AS name, count(p) AS paper_count
ORDER BY paper_count DESC
LIMIT $limit
"""


def paper_count() -> int:
    """Total number of papers in the graph."""
    return run_read(_PAPER_COUNT)[0]["paper_count"]


def latest_papers(limit: int = 10) -> list[dict]:
    """The most recently published papers, with their authors."""
    rows = run_read(_LATEST_PAPERS, limit=limit)
    for row in rows:
        row["authors"] = ", ".join(a for a in row["authors"] if a)
    return rows


def top_authors(limit: int = 10) -> list[dict]:
    """Authors with the most papers, by AUTHORED edge count."""
    return run_read(_TOP_AUTHORS, limit=limit)
