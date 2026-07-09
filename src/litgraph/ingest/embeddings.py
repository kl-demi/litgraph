from functools import lru_cache

from litgraph.config import get_settings


@lru_cache
def _get_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(get_settings().embedding_model_name)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of title+abstract strings. Loads the model lazily on first call."""
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return [v.tolist() for v in vectors]


def paper_embedding_text(title: str, abstract: str) -> str:
    return f"{title.strip()}\n{abstract.strip()}".strip()
