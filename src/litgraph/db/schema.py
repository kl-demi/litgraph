from arxiv_graphdb.config import get_settings
from arxiv_graphdb.db import arcadedb_http
from arxiv_graphdb.db.neo4j_client import run_write

_ARCADEDB_VERTEX_TYPES = ["Paper", "Category", "Author"]
_ARCADEDB_EDGE_TYPES = ["CITES", "IN_CATEGORY", "AUTHORED"]

_ARCADEDB_UNIQUE_INDEXES = [
    ("Paper", "id", "STRING"),
    ("Category", "code", "STRING"),
    ("Author", "name", "STRING"),
]

_ARCADEDB_RANGE_INDEXES = [
    ("Paper", "arxiv_id", "STRING"),
    ("Paper", "s2_paper_id", "STRING"),
    ("Paper", "enriched_at", "DATETIME"),
]

_CONSTRAINTS = [
    "CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT category_code IF NOT EXISTS FOR (c:Category) REQUIRE c.code IS UNIQUE",
    "CREATE CONSTRAINT author_name IF NOT EXISTS FOR (a:Author) REQUIRE a.name IS UNIQUE",
]

_RANGE_INDEXES = [
    "CREATE INDEX paper_arxiv_id IF NOT EXISTS FOR (p:Paper) ON (p.arxiv_id)",
    "CREATE INDEX paper_s2_id IF NOT EXISTS FOR (p:Paper) ON (p.s2_paper_id)",
    "CREATE INDEX paper_enriched_at IF NOT EXISTS FOR (p:Paper) ON (p.enriched_at)",
]

_FULLTEXT_INDEX = """
CREATE FULLTEXT INDEX paper_fulltext IF NOT EXISTS
FOR (p:Paper) ON EACH [p.title, p.abstract]
"""

_VECTOR_INDEX = """
CREATE VECTOR INDEX paper_embedding IF NOT EXISTS
FOR (p:Paper) ON (p.embedding)
OPTIONS {{indexConfig: {{
    `vector.dimensions`: {dimensions},
    `vector.similarity_function`: 'cosine'
}}}}
"""


def ensure_schema() -> None:
    """Idempotently create all constraints and indexes the pipeline relies on."""
    settings = get_settings()
    if settings.graph_backend == "neo4j":
        for stmt in [*_CONSTRAINTS, *_RANGE_INDEXES, _FULLTEXT_INDEX]:
            run_write(stmt)
        run_write(_VECTOR_INDEX.format(dimensions=settings.embedding_dimensions))
        return
    _ensure_arcadedb_schema(settings)


def _ensure_arcadedb_schema(settings) -> None:
    """ArcadeDB's Cypher layer has no DDL of its own (CREATE CONSTRAINT/INDEX aren't
    part of openCypher) — schema setup goes over the HTTP/SQL API instead."""
    arcadedb_http.ensure_database()

    for vertex_type in _ARCADEDB_VERTEX_TYPES:
        arcadedb_http.ensure_ddl(f"CREATE VERTEX TYPE {vertex_type} IF NOT EXISTS")
    for edge_type in _ARCADEDB_EDGE_TYPES:
        arcadedb_http.ensure_ddl(f"CREATE EDGE TYPE {edge_type} IF NOT EXISTS")

    for type_name, prop, prop_type in [*_ARCADEDB_UNIQUE_INDEXES, *_ARCADEDB_RANGE_INDEXES]:
        arcadedb_http.ensure_ddl(f"CREATE PROPERTY {type_name}.{prop} {prop_type}")
    arcadedb_http.ensure_ddl("CREATE PROPERTY Paper.title STRING")
    arcadedb_http.ensure_ddl("CREATE PROPERTY Paper.abstract STRING")
    arcadedb_http.ensure_ddl("CREATE PROPERTY Paper.embedding ARRAY_OF_FLOATS")

    for type_name, prop, _ in _ARCADEDB_UNIQUE_INDEXES:
        arcadedb_http.ensure_ddl(f"CREATE INDEX ON {type_name} ({prop}) UNIQUE")
    for type_name, prop, _ in _ARCADEDB_RANGE_INDEXES:
        arcadedb_http.ensure_ddl(f"CREATE INDEX ON {type_name} ({prop}) NOTUNIQUE")

    arcadedb_http.ensure_ddl("CREATE INDEX ON Paper (title, abstract) FULL_TEXT")
    arcadedb_http.ensure_ddl(
        "CREATE INDEX ON Paper (embedding) LSM_VECTOR METADATA "
        f'{{"dimensions": {settings.embedding_dimensions}, "similarity": "COSINE"}}'
    )
