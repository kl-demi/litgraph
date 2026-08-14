from litgraph.db.neo4j_client import run_read

# `id`, `pmid` and `arxiv_id` are each indexed, but an `OR` across two of them uses
# neither and degrades to a full Paper scan (21s on a 300k-paper graph). Resolve the key
# through one indexed lookup at a time instead, then traverse from the resolved node.
_RESOLVERS = (
    "MATCH (p:Paper) WHERE p.id = $key RETURN p.id AS id LIMIT 1",
    "MATCH (p:Paper) WHERE p.pmid = $key RETURN p.id AS id LIMIT 1",
    "MATCH (p:Paper) WHERE p.arxiv_id = $key RETURN p.id AS id LIMIT 1",
)

_REFERENCES = """
MATCH (p:Paper {id: $paper_id})-[:CITES]->(cited)
RETURN cited.id AS id, cited.arxiv_id AS arxiv_id, cited.pmid AS pmid, cited.title AS title,
       cited.is_stub AS is_stub, cited.citation_count AS citation_count
LIMIT $limit
"""

# The anchor is written first: with an unlabelled peer the planner starts from the
# leftmost element, so `(citing)-[:CITES]->(p:Paper {id: ...})` scans every vertex (29s).
_CITED_BY = """
MATCH (p:Paper {id: $paper_id})<-[:CITES]-(citing)
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


def resolve_paper_id(paper_id: str) -> str | None:
    """The canonical ``id`` for an arXiv id, a PMID, or an already-canonical id."""
    for query in _RESOLVERS:
        rows = run_read(query, key=paper_id)
        if rows:
            return rows[0]["id"]
    return None


def get_references(paper_id: str, limit: int = 50) -> list[dict]:
    """Papers that the paper identified by ``paper_id`` (an arXiv id or a PMID) cites."""
    resolved = resolve_paper_id(paper_id)
    return run_read(_REFERENCES, paper_id=resolved, limit=limit) if resolved else []


def get_citing_papers(paper_id: str, limit: int = 50) -> list[dict]:
    """Papers that cite the paper identified by ``paper_id`` (an arXiv id or a PMID)."""
    resolved = resolve_paper_id(paper_id)
    return run_read(_CITED_BY, paper_id=resolved, limit=limit) if resolved else []


def citation_neighborhood(paper_id: str, depth: int = 1, limit: int = 100) -> list[dict]:
    """Papers within ``depth`` CITES hops of ``paper_id``, in either direction.

    ``depth`` is clamped to ``[1, 3]``.
    """
    depth = max(1, min(int(depth), 3))
    resolved = resolve_paper_id(paper_id)
    if not resolved:
        return []
    query = f"""
    MATCH (p:Paper {{id: $paper_id}})-[:CITES*1..{depth}]-(other)
    RETURN DISTINCT other.id AS id, other.arxiv_id AS arxiv_id, other.pmid AS pmid, other.title AS title,
           other.is_stub AS is_stub
    LIMIT $limit
    """
    return run_read(query, paper_id=resolved, limit=limit)


def most_cited(category: str | None = None, limit: int = 20) -> list[dict]:
    """Most-cited papers, optionally restricted to one category.

    ``category`` must be a fully namespaced code (``arxiv:cs.CL``, ``mesh:D009422``).
    """
    return run_read(_MOST_CITED, category=category, limit=limit)
