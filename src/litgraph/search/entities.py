"""Name-substring lookup across the entity types, for the dashboard's search page."""

from litgraph.db.neo4j_client import run_read

# Label -> the property holding its natural key. Doubles as the allowlist: a label is
# looked up here before being interpolated into Cypher, which can't parameterize one.
# A label absent from the current database matches nothing rather than erroring
# (Trait exists only in the rice graph), so callers need no capability check.
ENTITY_KEYS = {
    "Gene": "gene_id",
    "Pathway": "pathway_id",
    "Trait": "trait_id",
    "Compound": "compound_id",
}


def search_entities(label: str, name_substr: str, limit: int = 10) -> list[dict]:
    """Entities of ``label`` whose name contains ``name_substr``, case-insensitive."""
    key = ENTITY_KEYS[label]
    return run_read(
        f"MATCH (n:{label}) WHERE toLower(n.name) CONTAINS toLower($q) "
        f"RETURN n.{key} AS id, n.name AS name ORDER BY size(n.name) LIMIT $limit",
        q=name_substr,
        limit=limit,
    )
