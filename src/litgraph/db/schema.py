"""Core paper-graph types, declared into the shared schema registry."""

from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_write
from litgraph.db.registry import EdgeType, NodeType, Prop, PropType, arcadedb_ddl, neo4j_ddl, register, registry
from litgraph.models import PAPER_IDENTIFIERS

# A Prop for each identifier scheme, to supply to the PAPER NodeType below.
_IDENTIFIER_PROPS = tuple(Prop(ns.column, PropType.STRING, indexed=True) for ns in PAPER_IDENTIFIERS)

PAPER = NodeType(
    name="Paper",
    key="id",
    props=(
        *_IDENTIFIER_PROPS,
        Prop("enriched_at", PropType.DATETIME, indexed=True),
        Prop("is_stub", PropType.BOOLEAN, indexed=True),
    ),
    fulltext=("title", "abstract"),
    vector="embedding",
)

CATEGORY = NodeType(
    name="Category",
    key="code",
    props=(
        Prop("vocabulary"),
        Prop("name"),
    ),
)

AUTHOR = NodeType(name="Author", key="name")
"""A paper's author, keyed on name. No disambiguation across name collisions."""

GRAPH_STATS = NodeType(name="GraphStats", key="id")
"""A singleton holding the counters for `stats overview`, so it never has to full-scan
the graph."""

CITES = EdgeType("CITES", src="Paper", dst="Paper")

IN_CATEGORY = EdgeType("IN_CATEGORY", src="Paper", dst="Category")

AUTHORED = EdgeType("AUTHORED", src="Author", dst="Paper")

register(PAPER, CATEGORY, AUTHOR, GRAPH_STATS, CITES, IN_CATEGORY, AUTHORED)


def ensure_schema() -> None:
    """Idempotently create every registered type, constraint and index.

    Pull settings (which backend, embedding dimensions) and drive the ArcadeDB/Neo4j DDL.
    """
    settings = get_settings()
    if settings.graph_backend == "neo4j":
        for statement in neo4j_ddl(registry, settings.embedding_dimensions):
            run_write(statement)
        return

    arcadedb_http.ensure_database()
    for statement in arcadedb_ddl(registry, settings.embedding_dimensions):
        arcadedb_http.ensure_ddl(statement)
