"""One Pathway record and its neighbours, for the dashboard's Pathway page."""

from litgraph.db.neo4j_client import run_read

_PATHWAY = """
MATCH (p:Pathway {pathway_id: $id})
RETURN p.pathway_id AS pathway_id, p.name AS name, p.source_db AS source_db
"""

_GENES = """
MATCH (g:Gene)-[r:PARTICIPATES_IN]->(:Pathway {pathway_id: $id})
RETURN g.gene_id AS gene_id, g.name AS name, r.evidence_code AS evidence_code
ORDER BY g.name LIMIT $limit
"""

_COMPOUNDS = """
MATCH (:Pathway {pathway_id: $id})-[r:PRODUCES]->(c:Compound)
RETURN c.compound_id AS compound_id, c.name AS name, r.evidence_code AS evidence_code
ORDER BY c.name LIMIT $limit
"""

# Papers reach a pathway only through the genes they mention; the ones touching the
# most of its genes come first, as the strongest evidence for the pathway as a whole.
_PAPERS = """
MATCH (pa:Paper)-[:MENTIONS]->(g:Gene)-[:PARTICIPATES_IN]->(:Pathway {pathway_id: $id})
RETURN pa.id AS id, pa.title AS title, count(DISTINCT g) AS gene_count
ORDER BY gene_count DESC LIMIT $limit
"""


def get_pathway(pathway_id: str) -> dict | None:
    """The Pathway with ``pathway_id`` (e.g. ``GO:0048575``), or None if absent."""
    rows = run_read(_PATHWAY, id=pathway_id)
    return rows[0] if rows else None


def genes_in_pathway(pathway_id: str, limit: int = 50) -> list[dict]:
    return run_read(_GENES, id=pathway_id, limit=limit)


def compounds_produced(pathway_id: str, limit: int = 25) -> list[dict]:
    return run_read(_COMPOUNDS, id=pathway_id, limit=limit)


def papers_for_pathway(pathway_id: str, limit: int = 15) -> list[dict]:
    """Papers mentioning this pathway's genes, most genes-touched first."""
    return run_read(_PAPERS, id=pathway_id, limit=limit)
