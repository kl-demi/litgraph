import time
from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from litgraph.config import get_settings
from litgraph.db.neo4j_client import chunked, run_read, run_write
from litgraph.ingest._pubmed_xml import iter_pubmed_articles, parse_pubmed_article
from litgraph.models import Paper

_GET_CHECKPOINT = """
MATCH (s:IngestState {job: $job})
RETURN s.last_seen_date AS last_seen_date
"""

_SET_CHECKPOINT = """
MERGE (s:IngestState {job: $job})
SET s.last_seen_date = $last_seen_date, s.last_run_at = $last_run_at
"""

_EFETCH_BATCH_SIZE = 200


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def get_checkpoint(job: str = "pubmed_daily") -> datetime | None:
    rows = run_read(_GET_CHECKPOINT, job=job)
    if not rows or rows[0]["last_seen_date"] is None:
        return None
    value = rows[0]["last_seen_date"]
    if hasattr(value, "to_native"):
        return value.to_native()
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def set_checkpoint(last_seen_date: datetime, job: str = "pubmed_daily") -> None:
    run_write(
        _SET_CHECKPOINT,
        job=job,
        last_seen_date=last_seen_date.isoformat(),
        last_run_at=datetime.now(UTC).isoformat(),
    )


def _entrez_params() -> dict:
    settings = get_settings()
    params = {}
    if settings.ncbi_email:
        params["email"] = settings.ncbi_email
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    return params


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _esearch(client: httpx.Client, mesh_terms: str, since: datetime | None, max_results: int) -> list[str]:
    params = {
        **_entrez_params(),
        "db": "pubmed",
        "term": mesh_terms,
        "retmode": "json",
        "retmax": max_results,
        "sort": "pub+date",
        "datetype": "pdat",
    }
    if since is not None:
        params["mindate"] = since.strftime("%Y/%m/%d")
        params["maxdate"] = datetime.now(UTC).strftime("%Y/%m/%d")
    response = client.get("/esearch.fcgi", params=params)
    response.raise_for_status()
    return response.json()["esearchresult"].get("idlist", [])


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _efetch(client: httpx.Client, pmids: list[str]) -> bytes:
    response = client.post(
        "/efetch.fcgi",
        params={**_entrez_params(), "db": "pubmed", "retmode": "xml"},
        data={"id": ",".join(pmids)},
    )
    response.raise_for_status()
    return response.content


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _esearch_with_history(
    client: httpx.Client, mesh_terms: str, start_date: date | None, end_date: date | None
) -> tuple[str, str, int]:
    """Run esearch with ``usehistory=y`` so the full matching set is stored server-side
    (NCBI's history server), sidestepping esearch's own 10,000-result retmax cap --
    efetch can then page through the whole set via ``retstart``.

    Returns (web_env, query_key, total_count).
    """
    params = {
        **_entrez_params(),
        "db": "pubmed",
        "term": mesh_terms,
        "retmode": "json",
        "retmax": 0,
        "usehistory": "y",
        "datetype": "pdat",
    }
    if start_date is not None:
        params["mindate"] = start_date.strftime("%Y/%m/%d")
    if end_date is not None:
        params["maxdate"] = end_date.strftime("%Y/%m/%d")
    response = client.get("/esearch.fcgi", params=params)
    response.raise_for_status()
    result = response.json()["esearchresult"]
    return result["webenv"], result["querykey"], int(result["count"])


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _efetch_history_batch(client: httpx.Client, web_env: str, query_key: str, retstart: int, retmax: int) -> bytes:
    response = client.post(
        "/efetch.fcgi",
        params={**_entrez_params(), "db": "pubmed", "retmode": "xml"},
        data={"WebEnv": web_env, "query_key": query_key, "retstart": retstart, "retmax": retmax},
    )
    response.raise_for_status()
    return response.content


def _result_to_paper(fields: dict) -> Paper:
    return Paper(
        pmid=fields["pmid"],
        title=fields["title"].replace("\n", " ").strip(),
        abstract=fields["abstract"].replace("\n", " ").strip(),
        authors=fields["authors"],
        categories=fields["categories"],
        primary_category=fields["primary_category"],
        published_date=fields["published_date"],
        updated_date=fields["updated_date"],
        doi=fields["doi"],
        journal_ref=fields["journal_ref"],
        comments=fields["comments"],
        source="pubmed",
        fetched_at=datetime.now(UTC),
    )


def fetch_new_papers(
    mesh_terms: str,
    since: datetime | None = None,
    max_results: int = 2000,
) -> Iterator[Paper]:
    """Fetch PubMed papers matching ``mesh_terms``, published since ``since``, via NCBI
    E-utilities (esearch to list PMIDs, efetch to pull full records).

    Requires ``ncbi_email``/``ncbi_api_key`` to be set for reasonable rate limits.
    """
    settings = get_settings()
    with httpx.Client(base_url=settings.ncbi_eutils_base_url, timeout=30.0) as client:
        pmids = _esearch(client, mesh_terms, since, max_results)
        for batch in chunked(pmids, _EFETCH_BATCH_SIZE):
            xml_bytes = _efetch(client, batch)
            for article_el in iter_pubmed_articles(xml_bytes):
                fields = parse_pubmed_article(article_el)
                if fields["pmid"]:
                    yield _result_to_paper(fields)


def fetch_historical_papers(
    mesh_terms: str,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 200,
) -> Iterator[Paper]:
    """Fetch *all* PubMed papers matching ``mesh_terms`` (optionally within a date range),
    via NCBI's history server (``usehistory=y`` + ``retstart`` pagination), rather than
    a single esearch call -- which caps at 10,000 results regardless of the requested
    ``retmax``. Intended for a full historical backload scoped by MeSH terms, as an
    alternative to downloading NCBI's bulk baseline files when disk space is tight,
    since NCBI filters by the query server-side before anything is sent.

    Requires ``ncbi_email``/``ncbi_api_key`` to be set; rate-limited to the NCBI-documented
    ceiling (10 req/sec with an API key, 3 req/sec without).
    """
    settings = get_settings()
    requests_per_second = 10.0 if settings.ncbi_api_key else 3.0
    min_interval = 1.0 / requests_per_second
    last_request_at: float | None = None

    with httpx.Client(base_url=settings.ncbi_eutils_base_url, timeout=30.0) as client:
        web_env, query_key, count = _esearch_with_history(client, mesh_terms, start_date, end_date)

        for retstart in range(0, count, batch_size):
            if last_request_at is not None:
                elapsed = time.monotonic() - last_request_at
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
            xml_bytes = _efetch_history_batch(client, web_env, query_key, retstart, batch_size)
            last_request_at = time.monotonic()

            for article_el in iter_pubmed_articles(xml_bytes):
                fields = parse_pubmed_article(article_el)
                if fields["pmid"]:
                    yield _result_to_paper(fields)
