"""Name/identifier-substring lookup across whatever entity types a database has.

Types are discovered from the live schema rather than listed here: a new corpus can
introduce a type (the human graph added Disease) and it becomes searchable without a
code change.
"""

from litgraph.db.arcadedb_http import run_query
from litgraph.db.neo4j_client import run_read

# Types that carry a name and a key but are not things a researcher searches for.
_NOT_SEARCHABLE = {"Paper", "Author", "Category", "GraphStats", "IngestState"}

# Searchable properties beyond name and the natural key, per type. locus_id is how rice
# researchers actually name a gene (LOC_Os09g25490).
_EXTRA_FIELDS = {"Gene": ("locus_id",)}


def searchable_types() -> dict[str, str]:
    """Vertex types worth searching, mapped to their natural key property.

    A type qualifies when it holds records, has a `name`, and has a single-property
    unique index -- which is what makes it an entity rather than a join table or a
    bookkeeping row.
    """
    found: dict[str, str] = {}
    for spec in run_query("select from schema:types where type = 'vertex'"):
        name = spec.get("name")
        if not name or not spec.get("records") or name in _NOT_SEARCHABLE:
            continue
        if not any(p.get("name") == "name" for p in spec.get("properties", [])):
            continue
        keys = [
            index["properties"][0]
            for index in spec.get("indexes", [])
            if index.get("unique") and len(index.get("properties", [])) == 1
        ]
        if keys:
            found[name] = keys[0]
    return found


def search_entities(label: str, query: str, limit: int = 10, key: str | None = None) -> list[dict]:
    """Entities of ``label`` whose name, id, or locus id contains ``query``,
    case-insensitive -- so a pasted ``TO:0000173`` or ``LOC_Os09g25490`` resolves, not
    just a name. A property that is null on a node simply doesn't match.

    ``label`` and ``key`` are interpolated into Cypher, which cannot parameterize
    either, so both come from the schema rather than from user input.
    """
    key = key or searchable_types().get(label)
    if not key:
        return []
    fields = ["n.name", f"n.{key}", *(f"n.{f}" for f in _EXTRA_FIELDS.get(label, ()))]
    condition = " OR ".join(f"toLower({f}) CONTAINS toLower($q)" for f in fields)
    return run_read(
        f"MATCH (n:{label}) WHERE {condition} "
        f"RETURN n.{key} AS id, n.name AS name ORDER BY size(n.name) LIMIT $limit",
        q=query,
        limit=limit,
    )
