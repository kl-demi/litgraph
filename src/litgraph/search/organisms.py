"""One Organism record and its neighbours, for the dashboard's Organism page.

Organism is reached only through MENTIONS, which is polymorphic: Paper points at Gene,
Compound and Organism with the same edge type, so the destination label carries the
whole distinction.
"""

from litgraph.db.neo4j_client import run_read

_ORGANISM = """
MATCH (o:Organism {taxon_id: $id})
RETURN o.taxon_id AS taxon_id, o.name AS name
"""

_PAPERS = """
MATCH (p:Paper)-[m:MENTIONS]->(:Organism {taxon_id: $id})
RETURN p.id AS id, p.title AS title, p.pmid AS pmid, m.source AS source
LIMIT $limit
"""

# Genes named in the same papers as this organism, most-shared first. Computed at query
# time rather than stored, the same shape as co-mentioned genes on the Gene page.
_GENES = """
MATCH (p:Paper)-[:MENTIONS]->(:Organism {taxon_id: $id})
MATCH (p)-[:MENTIONS]->(g:Gene)
RETURN g.gene_id AS gene_id, g.name AS name, count(DISTINCT p) AS shared_papers
ORDER BY shared_papers DESC LIMIT $limit
"""


def get_organism(taxon_id: str) -> dict | None:
    """The Organism with ``taxon_id`` (e.g. ``4530``), or None if absent."""
    rows = run_read(_ORGANISM, id=taxon_id)
    return rows[0] if rows else None


def papers_mentioning_organism(taxon_id: str, limit: int = 25) -> list[dict]:
    return run_read(_PAPERS, id=taxon_id, limit=limit)


def genes_for_organism(taxon_id: str, limit: int = 15) -> list[dict]:
    """Genes co-mentioned with this organism, by number of shared papers."""
    return run_read(_GENES, id=taxon_id, limit=limit)
