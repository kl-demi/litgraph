import time
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, stop_after_delay, wait_exponential

from litgraph.config import get_settings
from litgraph.db.neo4j_client import chunked
from litgraph.models import CitationStub, EnrichmentResult

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


def _wait_for_retry(retry_state) -> float:
    """On a 429, honor the server's Retry-After header instead of guessing via
    exponential backoff - S2 tells us exactly how long it wants us to wait."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return wait_exponential(multiplier=1, min=1, max=30)(retry_state)


def _stub_from(entry: dict | None) -> CitationStub | None:
    if not entry:
        return None
    external_ids = entry.get("externalIds") or {}
    arxiv_id = external_ids.get("ArXiv")
    pmid = external_ids.get("PubMed")
    s2_paper_id = entry.get("paperId")
    if not arxiv_id and not pmid and not s2_paper_id:
        return None
    return CitationStub(arxiv_id=arxiv_id, pmid=pmid, s2_paper_id=s2_paper_id, title=entry.get("title"))


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

    def enrich(self, pairs: list[tuple[str, str]], id_prefix: str) -> list[EnrichmentResult]:
        """Fetch citation data for a list of (paper_id, external_id) pairs, batching and
        rate-limiting internally. ``id_prefix`` is the Semantic Scholar external-id
        namespace for ``external_id`` (e.g. "ARXIV", "PMID")."""
        results: list[EnrichmentResult] = []
        for batch in chunked(pairs, self._batch_size):
            results.extend(self._enrich_batch(batch, id_prefix))
        return results

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=_wait_for_retry,
        stop=stop_after_attempt(8) | stop_after_delay(180),
        reraise=True,
    )
    def _post_batch(self, external_ids: list[str], id_prefix: str) -> list[dict | None]:
        self._throttle()
        response = self._client.post(
            "/paper/batch",
            params={"fields": _FIELDS},
            json={"ids": [f"{id_prefix}:{i}" for i in external_ids]},
        )
        if response.status_code == 400 and response.json().get("error") == "No valid paper ids given":
            # Whole batch unrecognized by Semantic Scholar - e.g. very recently
            # published papers it hasn't indexed yet. S2 returns this as a batch
            # of not-found results.
            return [None] * len(external_ids)
        response.raise_for_status()
        return response.json()

    def _enrich_batch(self, pairs: list[tuple[str, str]], id_prefix: str) -> list[EnrichmentResult]:
        items = self._post_batch([external_id for _, external_id in pairs], id_prefix)
        enriched_at = datetime.now(UTC)
        out: list[EnrichmentResult] = []
        for (paper_id, _), item in zip(pairs, items, strict=True):
            # Always emit a result -- even a "not found in S2" paper must get
            # `enriched_at` stamped, or it keeps reappearing at the front of
            # _FIND_UNENRICHED's result window on every future `enrich` run,
            # starving papers that haven't been attempted yet.
            if not item:
                out.append(EnrichmentResult(paper_id=paper_id, enriched_at=enriched_at))
                continue
            references = [s for s in (_stub_from(r) for r in item.get("references") or []) if s]
            citations = [s for s in (_stub_from(c) for c in item.get("citations") or []) if s]
            out.append(
                EnrichmentResult(
                    paper_id=paper_id,
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
