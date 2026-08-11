"""Name/identifier-substring lookup across the entity types, for the dashboard's search page."""

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

# Searchable properties beyond name and the natural key. locus_id is how rice
# researchers actually name a gene (LOC_Os09g25490).
_EXTRA_FIELDS = {"Gene": ("locus_id",)}


def search_entities(label: str, query: str, limit: int = 10) -> list[dict]:
    """Entities of ``label`` whose name, id, or locus id contains ``query``,
    case-insensitive — so a pasted ``TO:0000173`` or ``LOC_Os09g25490`` resolves,
    not just a name. A property that is null on a node simply doesn't match."""
    key = ENTITY_KEYS[label]
    fields = ["n.name", f"n.{key}", *(f"n.{f}" for f in _EXTRA_FIELDS.get(label, ()))]
    condition = " OR ".join(f"toLower({f}) CONTAINS toLower($q)" for f in fields)
    return run_read(
        f"MATCH (n:{label}) WHERE {condition} "
        f"RETURN n.{key} AS id, n.name AS name ORDER BY size(n.name) LIMIT $limit",
        q=query,
        limit=limit,
    )
