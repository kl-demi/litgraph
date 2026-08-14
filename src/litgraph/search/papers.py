"""One Paper record and its immediate neighbours, for the dashboard's Paper page."""

from litgraph.db.neo4j_client import run_read

# The anchor node is named even where the name is unused: ArcadeDB's Cypher layer
# only resolves an inline key predicate through the index on a *named* node, and
# full-scans the type otherwise (20s vs 0.1s on a 300k-paper graph).
_PAPER = """
MATCH (p:Paper {id: $id})
RETURN p.id AS id, p.title AS title, p.abstract AS abstract, p.published_date AS published_date,
       p.source AS source, p.doi AS doi, p.pmid AS pmid, p.arxiv_id AS arxiv_id,
       p.journal_ref AS journal_ref, p.is_stub AS is_stub
"""

_AUTHORS = "MATCH (a:Author)-[:AUTHORED]->(p:Paper {id: $id}) RETURN a.name AS name LIMIT $limit"

_GENES = """
MATCH (p:Paper {id: $id})-[m:MENTIONS]->(g:Gene)
RETURN g.gene_id AS gene_id, g.name AS name, m.source AS source
ORDER BY g.name LIMIT $limit
"""

_CATEGORIES = """
MATCH (p:Paper {id: $id})-[:IN_CATEGORY]->(c:Category)
RETURN c.code AS code, c.name AS name, c.vocabulary AS vocabulary
ORDER BY c.name LIMIT $limit
"""


def get_paper(paper_id: str) -> dict | None:
    """The Paper with ``paper_id`` (e.g. ``pmid:34437813``), or None if absent."""
    rows = run_read(_PAPER, id=paper_id)
    return rows[0] if rows else None


def authors_of(paper_id: str, limit: int = 50) -> list[dict]:
    return run_read(_AUTHORS, id=paper_id, limit=limit)


def genes_in(paper_id: str, limit: int = 50) -> list[dict]:
    """Genes the paper mentions, as extracted by PubTator."""
    return run_read(_GENES, id=paper_id, limit=limit)


def categories_of(paper_id: str, limit: int = 25) -> list[dict]:
    """Subject terms (MeSH, arXiv categories) the paper is filed under."""
    return run_read(_CATEGORIES, id=paper_id, limit=limit)
