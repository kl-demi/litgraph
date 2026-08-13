"""Generic node/edge upserts for any type declared in the schema registry.

Exports:
    upsert_nodes(node_type, rows, update_existing): insert-or-update nodes by natural key.
    upsert_edges(edge_type, rows, create_missing, update_existing): connect existing nodes.
    CreateMissing: enum choosing which absent edge endpoints get a key-only insert.

Emits SQL for the ArcadeDB backend and Cypher for Neo4j.

Usage:
    upsert_nodes("Pathway", [{"pathway_id": "GO:1", "name": "..."}], update_existing=True)
    upsert_edges("CITES", [{"src": "arxiv:a", "dst": "arxiv:b"}],
                 create_missing=CreateMissing.NONE, update_existing=False)
"""

from enum import StrEnum
from typing import Any

from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_write
from litgraph.db.registry import EdgeType, NodeType, registry

SRC = "src"
DST = "dst"


def _single(declared: str | tuple[str, ...], edge_name: str, side: str) -> str:
    """The endpoint's node type, or raise if it's multi-typed and needs an override."""
    if isinstance(declared, tuple):
        raise ValueError(
            f"{edge_name} declares multiple {side} types ({', '.join(declared)}); pass {side}= explicitly"
        )
    return declared


class CreateMissing(StrEnum):
    """Which absent edge endpoints get a key-only INSERT; rows with other absent
    endpoints are dropped. Existing nodes are matched and left untouched either way.

    Only endpoints whose NodeType declares `bootstrappable=True` may be created --
    `upsert_edges` raises otherwise. See NodeType.bootstrappable for the criteria.
    """

    NONE = "none"
    SRC = "src"
    DST = "dst"
    BOTH = "both"

    def creates(self, endpoint: str) -> bool:
        return self is CreateMissing.BOTH or self.value == endpoint


def upsert_nodes(node_type: str, rows: list[dict], *, update_existing: bool) -> int:
    """Upsert nodes of one type, keyed on the registry's declared key property.

    Args:
        node_type: A registered NodeType name, e.g. "Pathway".
        rows: One dict per node: the key property plus any registered properties.
        update_existing: Rewrite properties on match. False when another job may have
            written better values; True when this loader is the authority.

    Returns:
        int: Count of newly created nodes.
    """
    if not rows:
        return 0
    node = registry.node(node_type)
    props = _written_props(node, rows)
    if get_settings().graph_backend == "neo4j":
        return _run_cypher(_cypher_nodes(node, props, update_existing), rows=rows)
    return _run_script(_sql_nodes(node, props, update_existing), rows=rows)


def upsert_edges(
    edge_type: str,
    rows: list[dict],
    *,
    create_missing: CreateMissing,
    update_existing: bool,
    src: str | None = None,
    dst: str | None = None,
) -> int:
    """Upsert edges of one type between nodes matched on their registry keys.

    Args:
        edge_type: A registered EdgeType name, e.g. "PARTICIPATES_IN".
        rows: One dict per edge: "src"/"dst" key values plus any registered edge
            properties. Fixed names, so a self-edge (CITES: Paper->Paper) doesn't collide.
        create_missing: Which absent endpoints get a key-only insert.
        update_existing: Rewrite edge properties on match.
        src: Override the registered source node type. Required if the edge declares
            more than one source type (see EdgeType.src).
        dst: Override the registered destination node type. Required if the edge
            declares more than one destination type (see EdgeType.dst) -- MENTIONS,
            for instance, is declared against Gene/Compound/Organism/Disease.

    Returns:
        int: Count of newly created edges.

    Raises:
        ValueError: If `create_missing` targets an endpoint whose NodeType is not
            declared `bootstrappable`, or if a multi-typed endpoint has no override.
    """
    if not rows:
        return 0
    edge = registry.edges[edge_type]
    src_node = registry.node(src or _single(edge.src, edge.name, "src"))
    dst_node = registry.node(dst or _single(edge.dst, edge.name, "dst"))
    for endpoint, node in ((SRC, src_node), (DST, dst_node)):
        if create_missing.creates(endpoint) and not node.bootstrappable:
            raise ValueError(f"{edge.name} cannot bootstrap {node.name}: not declared bootstrappable")
    props = _written_props(edge, rows)
    if get_settings().graph_backend == "neo4j":
        query = _cypher_edges(edge, src_node, dst_node, props, create_missing, update_existing)
        return _run_cypher(query, rows=rows)
    query = _sql_edges(edge, src_node, dst_node, props, create_missing, update_existing)
    return _run_script(query, rows=rows)


def _written_props(type_: NodeType | EdgeType, rows: list[dict]) -> list[str]:
    """Registered property names the rows actually carry, so an absent optional property
    isn't written as null."""
    present = {key for row in rows for key in row}
    return [prop.name for prop in type_.props if prop.name in present]


def _run_script(sql: str, **params: Any) -> int:
    return int(arcadedb_http.run_script(sql, **params)[0]["value"])


def _run_cypher(cypher: str, **params: Any) -> int:
    return int(run_write(cypher, **params)[0]["created"])


# --- ArcadeDB SQL -----------------------------------------------------------------------
# `IF` has no `ELSE` in ArcadeDB's sqlscript, so an update-on-match is expressed as an
# unconditional UPDATE after the insert branch rather than as the other half of a branch.


def _sql_nodes(node: NodeType, props: list[str], update_existing: bool) -> str:
    assignments = ", ".join(f"{prop} = $r.{prop}" for prop in props)
    insert_tail = f", {assignments}" if assignments else ""
    update = f"  UPDATE {node.name} SET {assignments} WHERE {node.key} = $r.{node.key};" if (
        update_existing and assignments
    ) else ""
    return f"""
BEGIN;
LET created = 0;
FOREACH ($r IN :rows) {{
  LET existing = SELECT FROM {node.name} WHERE {node.key} = $r.{node.key};
  IF ($existing.size() = 0) {{
    INSERT INTO {node.name} SET {node.key} = $r.{node.key}{insert_tail};
    LET created = $created + 1;
  }}
{update}
}}
COMMIT;
RETURN $created;
"""


def _sql_endpoint(node: NodeType, endpoint: str, create_missing: CreateMissing) -> str:
    """SELECT one endpoint's rows, inserting a key-only node first when the policy allows."""
    var = f"{endpoint}Rows"
    select = f"LET {var} = SELECT FROM {node.name} WHERE {node.key} = $r.{endpoint};"
    if not create_missing.creates(endpoint):
        return f"  {select}"
    return (
        f"  {select}\n"
        f"  IF (${var}.size() = 0) {{\n"
        f"    INSERT INTO {node.name} SET {node.key} = $r.{endpoint};\n"
        f"    LET {var} = SELECT FROM {node.name} WHERE {node.key} = $r.{endpoint};\n"
        f"  }}"
    )


def _sql_edges(
    edge: EdgeType,
    src_node: NodeType,
    dst_node: NodeType,
    props: list[str],
    create_missing: CreateMissing,
    update_existing: bool,
) -> str:
    assignments = ", ".join(f"{prop} = $r.{prop}" for prop in props)
    create_tail = f" SET {assignments}" if assignments else ""
    update = (
        # No "EDGE" keyword: on this server (confirmed on ArcadeDB 26.8.1) "UPDATE EDGE
        # <type> SET ... WHERE ..." raises SchemaException "Type with name 'EDGE' was
        # not found" -- the parser resolves "EDGE" itself as the target type instead of
        # treating it as the vertex/edge disambiguator. Dropping it is unambiguous since
        # edge and vertex type names share one namespace; "UPDATE {type} SET ..." alone
        # resolves correctly regardless of which kind {type} is.
        f"      UPDATE {edge.name} SET {assignments} WHERE @out = $srcRid AND @in = $dstRid;"
        if update_existing and assignments
        else ""
    )
    return f"""
BEGIN;
LET created = 0;
FOREACH ($r IN :rows) {{
{_sql_endpoint(src_node, SRC, create_missing)}
{_sql_endpoint(dst_node, DST, create_missing)}
  IF ($srcRows.size() > 0 AND $dstRows.size() > 0) {{
    LET srcRid = $srcRows[0].@rid;
    LET dstRid = $dstRows[0].@rid;
    LET existing = SELECT FROM {edge.name} WHERE @out = $srcRid AND @in = $dstRid;
    IF ($existing.size() = 0) {{
      CREATE EDGE {edge.name} FROM $srcRid TO $dstRid{create_tail};
      LET created = $created + 1;
    }}
{update}
  }}
}}
COMMIT;
RETURN $created;
"""


# --- Neo4j Cypher -----------------------------------------------------------------------
# `_is_new` sentinels rather than counting MERGEs: they are the only way to tell a created
# node from a matched one, and are removed before the query returns.


def _cypher_nodes(node: NodeType, props: list[str], update_existing: bool) -> str:
    assignments = ", ".join(f"n.{prop} = r.{prop}" for prop in props)
    on_create = f"ON CREATE SET n._is_new = true, {assignments}" if assignments else "ON CREATE SET n._is_new = true"
    update = f"SET {assignments}" if update_existing and assignments else ""
    return f"""
UNWIND $rows AS r
MERGE (n:{node.name} {{{node.key}: r.{node.key}}})
{on_create}
WITH n, r, coalesce(n._is_new, false) AS is_new
REMOVE n._is_new
{update}
RETURN count(CASE WHEN is_new THEN 1 END) AS created
"""


def _cypher_edges(
    edge: EdgeType,
    src_node: NodeType,
    dst_node: NodeType,
    props: list[str],
    create_missing: CreateMissing,
    update_existing: bool,
) -> str:
    def endpoint(var: str, node: NodeType, side: str) -> str:
        clause = "MERGE" if create_missing.creates(side) else "MATCH"
        return f"{clause} ({var}:{node.name} {{{node.key}: r.{side}}})"

    assignments = ", ".join(f"e.{prop} = r.{prop}" for prop in props)
    on_create = f"ON CREATE SET e._is_new = true, {assignments}" if assignments else "ON CREATE SET e._is_new = true"
    update = f"SET {assignments}" if update_existing and assignments else ""
    return f"""
UNWIND $rows AS r
{endpoint("s", src_node, SRC)}
WITH s, r
{endpoint("d", dst_node, DST)}
MERGE (s)-[e:{edge.name}]->(d)
{on_create}
WITH e, r, coalesce(e._is_new, false) AS is_new
REMOVE e._is_new
{update}
RETURN count(CASE WHEN is_new THEN 1 END) AS created
"""
