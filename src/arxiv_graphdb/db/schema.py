from arxiv_graphdb.config import get_settings
from arxiv_graphdb.db.neo4j_client import run_write

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
    for stmt in [*_CONSTRAINTS, *_RANGE_INDEXES, _FULLTEXT_INDEX]:
        run_write(stmt)
    run_write(_VECTOR_INDEX.format(dimensions=settings.embedding_dimensions))
