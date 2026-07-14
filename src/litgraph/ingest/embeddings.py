from functools import lru_cache

from tenacity import retry, retry_if_exception, stop_after_attempt, stop_after_delay, wait_exponential

from litgraph.config import get_settings


def _is_retryable_embedding_error(exc: BaseException) -> bool:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


class _AdapterEmbedder:
    """Wraps SPECTER2 (base model + proximity adapter) behind an .encode() interface,
    since it's an AdapterHub model rather than a plain sentence-transformers checkpoint.
    """

    def __init__(self, base_model_name: str, adapter_name: str):
        import torch
        from adapters import AutoAdapterModel
        from transformers import AutoTokenizer

        if torch.cuda.is_available():
            self._device = "cuda"
        elif torch.backends.mps.is_available():
            self._device = "mps"
        else:
            self._device = "cpu"

        self._tokenizer = AutoTokenizer.from_pretrained(base_model_name)
        self._model = AutoAdapterModel.from_pretrained(base_model_name)
        self._model.load_adapter(adapter_name, source="hf", set_active=True)
        self._model.to(self._device)
        self._model.eval()

    def encode(self, texts, batch_size=32, normalize_embeddings=True, **_kwargs):
        import torch

        all_vectors = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            inputs = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            ).to(self._device)
            with torch.no_grad():
                outputs = self._model(**inputs)
            # SPECTER2 embeddings are the [CLS] token of the last hidden state.
            cls_embeddings = outputs.last_hidden_state[:, 0, :]
            if normalize_embeddings:
                cls_embeddings = torch.nn.functional.normalize(cls_embeddings, p=2, dim=1)
            all_vectors.extend(cls_embeddings.tolist())
        return all_vectors


@lru_cache
def _get_model():
    settings = get_settings()
    return _AdapterEmbedder(settings.embedding_model_name, settings.embedding_adapter_name)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of title+abstract strings. Delegates to a remote GPU embedding
    server if settings.embedding_service_url is set; otherwise loads the model
    in-process, lazily, on first call."""
    if not texts:
        return []
    settings = get_settings()
    if settings.embedding_service_url:
        return _embed_remote(texts, settings.embedding_service_url)
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    return [list(v) for v in vectors]


@retry(
    retry=retry_if_exception(_is_retryable_embedding_error),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(6) | stop_after_delay(180),
    reraise=True,
)
def _embed_remote(texts: list[str], service_url: str) -> list[list[float]]:
    import httpx

    settings = get_settings()
    headers = {}
    if settings.embedding_service_token:
        headers["Authorization"] = f"Bearer {settings.embedding_service_token}"
    response = httpx.post(
        f"{service_url}/embed", json={"texts": texts}, headers=headers, timeout=120
    )
    response.raise_for_status()
    return response.json()["vectors"]


def paper_embedding_text(title: str, abstract: str) -> str:
    return f"{title.strip()}\n{abstract.strip()}".strip()
