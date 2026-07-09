from arxiv_graphdb.db.neo4j_client import run_read

_REFERENCES = """
MATCH (p:Paper {arxiv_id: $arxiv_id})-[:CITES]->(cited)
RETURN cited.id AS id, cited.arxiv_id AS arxiv_id, cited.title AS title,
       cited.is_stub AS is_stub, cited.citation_count AS citation_count
LIMIT $limit
"""

_CITED_BY = """
MATCH (citing)-[:CITES]->(p:Paper {arxiv_id: $arxiv_id})
RETURN citing.id AS id, citing.arxiv_id AS arxiv_id, citing.title AS title,
       citing.is_stub AS is_stub, citing.citation_count AS citation_count
LIMIT $limit
"""

_MOST_CITED = """
MATCH (p:Paper)
WHERE p.is_stub = false AND p.citation_count IS NOT NULL
  AND ($category IS NULL OR $category IN p.categories)
RETURN p.arxiv_id AS arxiv_id, p.title AS title, p.citation_count AS citation_count
ORDER BY p.citation_count DESC
LIMIT $limit
"""


def get_references(arxiv_id: str, limit: int = 50) -> list[dict]:
    """Papers that ``arxiv_id`` cites."""
    return run_read(_REFERENCES, arxiv_id=arxiv_id, limit=limit)


def get_citing_papers(arxiv_id: str, limit: int = 50) -> list[dict]:
    """Papers that cite ``arxiv_id``."""
    return run_read(_CITED_BY, arxiv_id=arxiv_id, limit=limit)


def citation_neighborhood(arxiv_id: str, depth: int = 1, limit: int = 100) -> list[dict]:
    """Papers within ``depth`` CITES hops of ``arxiv_id``, in either direction."""
    depth = max(1, min(int(depth), 3))
    query = f"""
    MATCH (p:Paper {{arxiv_id: $arxiv_id}})-[:CITES*1..{depth}]-(other)
    RETURN DISTINCT other.id AS id, other.arxiv_id AS arxiv_id, other.title AS title,
           other.is_stub AS is_stub
    LIMIT $limit
    """
    return run_read(query, arxiv_id=arxiv_id, limit=limit)


def most_cited(category: str | None = None, limit: int = 20) -> list[dict]:
    return run_read(_MOST_CITED, category=category, limit=limit)
