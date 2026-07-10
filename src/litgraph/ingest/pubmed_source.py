from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

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


def _efetch(client: httpx.Client, pmids: list[str]) -> bytes:
    response = client.post(
        "/efetch.fcgi",
        params={**_entrez_params(), "db": "pubmed", "retmode": "xml"},
        data={"id": ",".join(pmids)},
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
