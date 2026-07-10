from litgraph.db.neo4j_client import run_read

_REFERENCES = """
MATCH (p:Paper) WHERE p.arxiv_id = $paper_id OR p.pmid = $paper_id
MATCH (p)-[:CITES]->(cited)
RETURN cited.id AS id, cited.arxiv_id AS arxiv_id, cited.pmid AS pmid, cited.title AS title,
       cited.is_stub AS is_stub, cited.citation_count AS citation_count
LIMIT $limit
"""

_CITED_BY = """
MATCH (p:Paper) WHERE p.arxiv_id = $paper_id OR p.pmid = $paper_id
MATCH (citing)-[:CITES]->(p)
RETURN citing.id AS id, citing.arxiv_id AS arxiv_id, citing.pmid AS pmid, citing.title AS title,
       citing.is_stub AS is_stub, citing.citation_count AS citation_count
LIMIT $limit
"""

_MOST_CITED = """
MATCH (p:Paper)
WHERE p.is_stub = false AND p.citation_count IS NOT NULL
  AND ($category IS NULL OR $category IN p.categories)
RETURN p.arxiv_id AS arxiv_id, p.pmid AS pmid, p.title AS title, p.citation_count AS citation_count
ORDER BY p.citation_count DESC
LIMIT $limit
"""


def get_references(paper_id: str, limit: int = 50) -> list[dict]:
    """Papers that the paper identified by ``paper_id`` (an arXiv id or a PMID) cites."""
    return run_read(_REFERENCES, paper_id=paper_id, limit=limit)


def get_citing_papers(paper_id: str, limit: int = 50) -> list[dict]:
    """Papers that cite the paper identified by ``paper_id`` (an arXiv id or a PMID)."""
    return run_read(_CITED_BY, paper_id=paper_id, limit=limit)


def citation_neighborhood(paper_id: str, depth: int = 1, limit: int = 100) -> list[dict]:
    """Papers within ``depth`` CITES hops of ``paper_id`` (an arXiv id or a PMID), in either direction."""
    depth = max(1, min(int(depth), 3))
    query = f"""
    MATCH (p:Paper) WHERE p.arxiv_id = $paper_id OR p.pmid = $paper_id
    MATCH (p)-[:CITES*1..{depth}]-(other)
    RETURN DISTINCT other.id AS id, other.arxiv_id AS arxiv_id, other.pmid AS pmid, other.title AS title,
           other.is_stub AS is_stub
    LIMIT $limit
    """
    return run_read(query, paper_id=paper_id, limit=limit)


def most_cited(category: str | None = None, limit: int = 20) -> list[dict]:
    return run_read(_MOST_CITED, category=category, limit=limit)
