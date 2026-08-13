"""A backend-agnostic declarative registry. It defines:
    - Declarative building blocks: `Prop`, `NodeType`, `EdgeType`;
    - DDL emitters for ArcadeDB and Neo4j;
    - The Registry instance that tracks what types a database has.

Any package can create new types as `NodeType`/`EdgeType` instances and call register()
to add them to the shared registry. This is done in `schema.py` (paper types) and
`spokebio.schema_ext` (biology types).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum


class PropType(StrEnum):
    """Property types, named by their ArcadeDB spelling.

    Neo4j declares no property types, so this only affects the ArcadeDB dialect.
    """

    STRING = "STRING"
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    LONG = "LONG"
    FLOAT = "FLOAT"
    DATE = "DATE"
    DATETIME = "DATETIME"
    FLOAT_ARRAY = "ARRAY_OF_FLOATS"


@dataclass(frozen=True)
class Prop:
    """One property on a node or edge type. ``indexed`` gives it a non-unique range index."""

    name: str
    type: PropType = PropType.STRING
    indexed: bool = False


@dataclass(frozen=True)
class NodeType:
    """A vertex type, its natural key, and every index hanging off it.

    ``key`` is the unique-indexed natural key: an identifier from an external source,
    not a synthetic id.

    ``bootstrappable`` is whether an edge upsert may create this node key-only when
    absent. True only if (a) ids are validated upstream (crosswalk/NER normalization)
    before reaching a writer, and (b) a key-only node is complete -- every other property
    is optional enrichment. Ontology terms fail both: the graph lookup is their only id
    validation, and their loader never fills in an obsolete term.
    """

    name: str
    key: str
    key_type: PropType = PropType.STRING
    props: tuple[Prop, ...] = ()
    fulltext: tuple[str, ...] = ()  # Properties in a combined full-text index, if any.
    vector: str | None = None  # Embedding vector property, if any. Dimensions come from
    # settings at emit time, not here, since they follow the embedding model.
    bootstrappable: bool = False

    def all_props(self) -> Iterator[Prop]:
        """Every property needing a declaration, key first."""
        yield Prop(self.key, self.key_type)
        yield from self.props


@dataclass(frozen=True)
class EdgeType:
    """An edge type and the node types it connects.

    ``src``/``dst`` let `upsert_edges`-style generic writers know which key to
    match each end on (docs/architecture.md §3). An endpoint that fans out to more than
    one node type (e.g. MENTIONS) declares a tuple; `upsert_edges` then requires the
    caller to disambiguate with an explicit `src=`/`dst=` override.
    """

    name: str
    src: str | tuple[str, ...]
    dst: str | tuple[str, ...]
    props: tuple[Prop, ...] = ()


@dataclass
class Registry:
    """The set of types a database should contain. Ordered, so DDL is emitted
    deterministically and node types always precede the edges referencing them."""

    nodes: dict[str, NodeType] = field(default_factory=dict)
    edges: dict[str, EdgeType] = field(default_factory=dict)

    def register(self, *types: NodeType | EdgeType) -> None:
        """Add types.

        Re-registering an identical type is a no-op, so a module can be imported twice.
        Re-registering a different type under the same name raises instead of silently
        keeping one of the two conflicting declarations.
        """
        for type_ in types:
            target = self.nodes if isinstance(type_, NodeType) else self.edges
            existing = target.get(type_.name)
            if existing is not None and existing != type_:
                raise ValueError(f"{type_.name} is already registered with a different definition")
            target[type_.name] = type_

    def node(self, name: str) -> NodeType:
        return self.nodes[name]

    def validate(self) -> None:
        """Check every edge's endpoints are registered node types.

        Neither backend rejects an edge pointing at an unknown node type, so a typo would
        otherwise surface later as a write that silently matches nothing.
        """
        for edge in self.edges.values():
            for declared in (edge.src, edge.dst):
                targets = declared if isinstance(declared, tuple) else (declared,)
                for endpoint in targets:
                    if endpoint not in self.nodes:
                        raise ValueError(f"edge {edge.name} references unregistered node type {endpoint}")


# The process-wide registry, populated by `litgraph.db.schema` at import time.
registry = Registry()
register = registry.register


def arcadedb_ddl(reg: Registry, embedding_dimensions: int) -> Iterator[str]:
    """Emit ArcadeDB DDL for every registered type, over the HTTP/SQL API.

    Only `CREATE VERTEX/EDGE TYPE` honours `IF NOT EXISTS`; `CREATE PROPERTY` and
    `CREATE INDEX` raise a parse error on it, so idempotency for those relies on
    `arcadedb_http.ensure_ddl` swallowing "already exists".
    """
    reg.validate()

    for node in reg.nodes.values():
        yield f"CREATE VERTEX TYPE {node.name} IF NOT EXISTS"
    for edge in reg.edges.values():
        yield f"CREATE EDGE TYPE {edge.name} IF NOT EXISTS"

    for node in reg.nodes.values():
        declared = set()
        for prop in node.all_props():
            declared.add(prop.name)
            yield f"CREATE PROPERTY {node.name}.{prop.name} {prop.type.value}"
        # Full-text/vector properties need declaring too when `props` doesn't already.
        for prop_name in node.fulltext:
            if prop_name not in declared:
                yield f"CREATE PROPERTY {node.name}.{prop_name} {PropType.STRING.value}"
        if node.vector and node.vector not in declared:
            yield f"CREATE PROPERTY {node.name}.{node.vector} {PropType.FLOAT_ARRAY.value}"

    for edge in reg.edges.values():
        for prop in edge.props:
            yield f"CREATE PROPERTY {edge.name}.{prop.name} {prop.type.value}"

    for node in reg.nodes.values():
        yield f"CREATE INDEX ON {node.name} ({node.key}) UNIQUE"
        for prop in node.props:
            if prop.indexed:
                yield f"CREATE INDEX ON {node.name} ({prop.name}) NOTUNIQUE"
        if node.fulltext:
            yield f"CREATE INDEX ON {node.name} ({', '.join(node.fulltext)}) FULL_TEXT"
        if node.vector:
            yield (
                f"CREATE INDEX ON {node.name} ({node.vector}) LSM_VECTOR METADATA "
                f'{{"dimensions": {embedding_dimensions}, "similarity": "COSINE"}}'
            )


def neo4j_ddl(reg: Registry, embedding_dimensions: int) -> Iterator[str]:
    """Emit Neo4j DDL for every registered type.

    Neo4j has no vertex/edge type or property-type declarations, so only constraints and
    indexes are emitted. Every statement is `IF NOT EXISTS`, so this needs no
    error-swallowing like the ArcadeDB path.
    """
    reg.validate()

    for node in reg.nodes.values():
        var = node.name[0].lower()
        yield (
            f"CREATE CONSTRAINT {_index_name(node.name, node.key)} IF NOT EXISTS "
            f"FOR ({var}:{node.name}) REQUIRE {var}.{node.key} IS UNIQUE"
        )
        for prop in node.props:
            if prop.indexed:
                yield (
                    f"CREATE INDEX {_index_name(node.name, prop.name)} IF NOT EXISTS "
                    f"FOR ({var}:{node.name}) ON ({var}.{prop.name})"
                )
        if node.fulltext:
            fields = ", ".join(f"{var}.{prop_name}" for prop_name in node.fulltext)
            yield (
                f"CREATE FULLTEXT INDEX {node.name.lower()}_fulltext IF NOT EXISTS "
                f"FOR ({var}:{node.name}) ON EACH [{fields}]"
            )
        if node.vector:
            yield (
                f"CREATE VECTOR INDEX {node.name.lower()}_{node.vector} IF NOT EXISTS "
                f"FOR ({var}:{node.name}) ON ({var}.{node.vector}) "
                "OPTIONS {indexConfig: {"
                f"`vector.dimensions`: {embedding_dimensions}, "
                "`vector.similarity_function`: 'cosine'}}"
            )


def _index_name(node_name: str, prop_name: str) -> str:
    """Neo4j index name, e.g. `Paper`/`arxiv_id` -> `paper_arxiv_id`.

    `id` is special-cased to `paper_id`, not `paper_id_id`.
    """
    prefix = node_name.lower()
    if prop_name == "id":
        return f"{prefix}_id"
    return f"{prefix}_{prop_name}"


def indexed_props(reg: Registry, node_name: str) -> Iterable[str]:
    """Every indexed property name on a node type, key included."""
    node = reg.node(node_name)
    return [node.key, *(p.name for p in node.props if p.indexed)]
