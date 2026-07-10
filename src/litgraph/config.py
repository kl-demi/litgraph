from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "arcadedb" (default) or "neo4j". This flag controls which SQL/Cypher dialect
    # schema.py, semantic.py, and keyword.py use for vector/full-text index creation
    # and querying, which are vendor-specific on both sides (not part of core Cypher).
    graph_backend: str = "arcadedb"

    arcadedb_uri: str = "bolt://localhost:7688"
    arcadedb_http_url: str = "http://localhost:2480"
    arcadedb_database: str = "litgraph"
    arcadedb_user: str = "root"
    arcadedb_password: str = "playwithdata"

    # Only used when graph_backend == "neo4j".
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme123"

    semantic_scholar_api_key: str | None = None
    semantic_scholar_base_url: str = "https://api.semanticscholar.org/graph/v1"
    semantic_scholar_batch_size: int = 500
    semantic_scholar_requests_per_second: float = 1.0

    embedding_model_name: str = "allenai/specter2_base"
    embedding_adapter_name: str = "allenai/specter2"
    embedding_dimensions: int = 768

    default_arxiv_categories: str = "cs.CL,cs.LG,cs.AI"

    kaggle_dataset_path: str | None = None

    default_pubmed_mesh_terms: str = (
        '"Anatomy"[MeSH Major Topic] OR "Phenomena and Processes"[MeSH Major Topic]'
    )
    pubmed_baseline_dir: str | None = None

    ncbi_email: str | None = None
    ncbi_api_key: str | None = None
    ncbi_eutils_base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    run_log_path: str = "logs/ingestion_runs.jsonl"

    @property
    def default_categories_list(self) -> list[str]:
        return [c.strip() for c in self.default_arxiv_categories.split(",") if c.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
