"""One Compound record and its neighbours, for the dashboard's Compound page."""

from litgraph.db.neo4j_client import run_read

_COMPOUND = """
MATCH (c:Compound {compound_id: $id})
RETURN c.compound_id AS compound_id, c.name AS name
"""

_PAPERS = """
MATCH (p:Paper)-[m:MENTIONS]->(:Compound {compound_id: $id})
RETURN p.id AS id, p.title AS title, p.pmid AS pmid, m.source AS source
LIMIT $limit
"""

_PATHWAYS = """
MATCH (pw:Pathway)-[r:PRODUCES]->(:Compound {compound_id: $id})
RETURN pw.pathway_id AS pathway_id, pw.name AS name, pw.source_db AS source_db,
       r.evidence_code AS evidence_code
ORDER BY pw.name LIMIT $limit
"""


def get_compound(compound_id: str) -> dict | None:
    """The Compound with ``compound_id`` (e.g. ``mesh:D009584``), or None if absent."""
    rows = run_read(_COMPOUND, id=compound_id)
    return rows[0] if rows else None


def papers_mentioning_compound(compound_id: str, limit: int = 25) -> list[dict]:
    return run_read(_PAPERS, id=compound_id, limit=limit)


def pathways_producing(compound_id: str, limit: int = 25) -> list[dict]:
    """Pathways recorded as producing this compound. Empty wherever PRODUCES is."""
    return run_read(_PATHWAYS, id=compound_id, limit=limit)
