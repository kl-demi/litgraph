"""One Trait record and its neighbours, for the dashboard's Trait page.

Trait and its ASSOCIATED_WITH edge come from Oryzabase and so far exist only in the
rice graph; on other databases these all match nothing rather than erroring.
"""

from litgraph.db.neo4j_client import run_read

_TRAIT = """
MATCH (t:Trait {trait_id: $id})
RETURN t.trait_id AS trait_id, t.name AS name, t.source_db AS source_db
"""

_GENES = """
MATCH (g:Gene)-[r:ASSOCIATED_WITH]->(:Trait {trait_id: $id})
RETURN g.gene_id AS gene_id, g.name AS name, r.source_db AS source_db
ORDER BY g.name LIMIT $limit
"""

_PAPERS = """
MATCH (pa:Paper)-[:MENTIONS]->(g:Gene)-[:ASSOCIATED_WITH]->(:Trait {trait_id: $id})
RETURN pa.id AS id, pa.title AS title, count(DISTINCT g) AS gene_count
ORDER BY gene_count DESC LIMIT $limit
"""


def get_trait(trait_id: str) -> dict | None:
    """The Trait with ``trait_id`` (e.g. ``TO:0000173``), or None if absent."""
    rows = run_read(_TRAIT, id=trait_id)
    return rows[0] if rows else None


def genes_for_trait(trait_id: str, limit: int = 50) -> list[dict]:
    return run_read(_GENES, id=trait_id, limit=limit)


def papers_for_trait(trait_id: str, limit: int = 15) -> list[dict]:
    """Papers mentioning this trait's associated genes, most genes-touched first."""
    return run_read(_PAPERS, id=trait_id, limit=limit)
