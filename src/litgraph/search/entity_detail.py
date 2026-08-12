"""One record of any vertex type, plus its neighbours, without knowing the type.

The per-type modules (genes, pathways, traits, compounds, organisms) exist because each
has sections worth curating. This is the fallback for everything else: a corpus can add
a type -- the human graph added Disease -- and it gets a page with no new code.
"""

from litgraph.db.neo4j_client import run_read

# Label and key names are interpolated because Cypher cannot parameterize either. Both
# come from the live schema via entities.searchable_types(), never from user input.
_RECORD = "MATCH (n:{label} {{{key}: $id}}) RETURN n AS record"


def get_record(label: str, key: str, entity_id: str) -> dict | None:
    """The record itself, as a plain property map, or None if absent."""
    rows = run_read(_RECORD.format(label=label, key=key), id=entity_id)
    if not rows:
        return None
    record = rows[0].get("record")
    return dict(record) if record else None


def neighbours(
    label: str, key: str, entity_id: str, key_map: dict[str, str], limit: int = 60
) -> list[dict]:
    """Everything one hop away, each row carrying its edge type and neighbour label.

    ``key_map`` maps every label that has a page to its natural key property, and the
    projection asks for all of them at once -- a neighbour's identifier lives under a
    different property depending on what it is, and one query cannot know in advance
    which it will hit.

    Direction is deliberately ignored: for a page showing what a thing connects to,
    MENTIONS arriving from a Paper and MENTIONS leaving for a Gene are the same fact.
    """
    # Deduplicated: several labels share a key name (Paper and GraphStats both use id).
    keys = sorted(set(key_map.values()))
    projection = ", ".join(f"m.{k} AS key_{k}" for k in keys)
    rows = run_read(
        f"MATCH (n:{label} {{{key}: $id}})-[r]-(m) "
        f"RETURN type(r) AS rel, labels(m) AS kinds, m.name AS name, m.title AS title, "
        f"{projection} LIMIT $limit",
        id=entity_id,
        limit=limit,
    )
    out = []
    for row in rows:
        kinds = row.get("kinds") or []
        kind = kinds[0] if kinds else "Node"
        neighbour_key = key_map.get(kind)
        out.append(
            {
                "rel": row.get("rel") or "",
                "kind": kind,
                "label": row.get("name") or row.get("title") or "",
                "id": row.get(f"key_{neighbour_key}") if neighbour_key else None,
            }
        )
    return out
