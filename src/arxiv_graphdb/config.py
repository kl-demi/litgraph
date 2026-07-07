from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:8008"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme123"

    semantic_scholar_api_key: str | None = None
    semantic_scholar_base_url: str = "https://api.semanticscholar.org/graph/v1"
    semantic_scholar_batch_size: int = 500
    semantic_scholar_requests_per_second: float = 1.0

    embedding_model_name: str = "sentence-transformers/allenai-specter"
    embedding_dimensions: int = 768

    default_arxiv_categories: str = "cs.CL,cs.LG,cs.AI"

    kaggle_dataset_path: str | None = None

    @property
    def default_categories_list(self) -> list[str]:
        return [c.strip() for c in self.default_arxiv_categories.split(",") if c.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
