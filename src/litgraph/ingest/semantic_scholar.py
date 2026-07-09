import time
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from arxiv_graphdb.config import get_settings
from arxiv_graphdb.db.neo4j_client import chunked
from arxiv_graphdb.models import CitationStub, EnrichmentResult

_FIELDS = ",".join(
    [
        "externalIds",
        "citationCount",
        "referenceCount",
        "influentialCitationCount",
        "references.externalIds",
        "references.title",
        "citations.externalIds",
        "citations.title",
    ]
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _stub_from(entry: dict | None) -> CitationStub | None:
    if not entry:
        return None
    arxiv_id = (entry.get("externalIds") or {}).get("ArXiv")
    s2_paper_id = entry.get("paperId")
    if not arxiv_id and not s2_paper_id:
        return None
    return CitationStub(arxiv_id=arxiv_id, s2_paper_id=s2_paper_id, title=entry.get("title"))


class SemanticScholarClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._batch_size = settings.semantic_scholar_batch_size
        self._min_interval = 1.0 / settings.semantic_scholar_requests_per_second
        headers = {}
        if settings.semantic_scholar_api_key:
            headers["x-api-key"] = settings.semantic_scholar_api_key
        self._client = httpx.Client(
            base_url=settings.semantic_scholar_base_url,
            headers=headers,
            timeout=30.0,
        )
        self._last_request_at: float | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SemanticScholarClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def enrich(self, arxiv_ids: list[str]) -> list[EnrichmentResult]:
        """Fetch citation data for a list of arXiv ids, batching and rate-limiting internally."""
        results: list[EnrichmentResult] = []
        for batch in chunked(arxiv_ids, self._batch_size):
            results.extend(self._enrich_batch(batch))
        return results

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _post_batch(self, ids: list[str]) -> list[dict | None]:
        self._throttle()
        response = self._client.post(
            "/paper/batch",
            params={"fields": _FIELDS},
            json={"ids": [f"ARXIV:{i}" for i in ids]},
        )
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 5))
            time.sleep(retry_after)
        response.raise_for_status()
        return response.json()

    def _enrich_batch(self, ids: list[str]) -> list[EnrichmentResult]:
        items = self._post_batch(ids)
        enriched_at = datetime.now(UTC)
        out: list[EnrichmentResult] = []
        for arxiv_id, item in zip(ids, items, strict=True):
            if not item:
                continue
            references = [s for s in (_stub_from(r) for r in item.get("references") or []) if s]
            citations = [s for s in (_stub_from(c) for c in item.get("citations") or []) if s]
            out.append(
                EnrichmentResult(
                    arxiv_id=arxiv_id,
                    s2_paper_id=item.get("paperId"),
                    citation_count=item.get("citationCount"),
                    reference_count=item.get("referenceCount"),
                    influential_citation_count=item.get("influentialCitationCount"),
                    references=references,
                    citations=citations,
                    enriched_at=enriched_at,
                )
            )
        return out
