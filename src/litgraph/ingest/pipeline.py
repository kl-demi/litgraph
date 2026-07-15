from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from litgraph.config import get_settings
from litgraph.db.neo4j_client import chunked, run_read
from litgraph.graph.upsert import apply_enrichment, set_paper_embeddings, upsert_papers
from litgraph.ingest.arxiv_source import fetch_new_papers, get_checkpoint, set_checkpoint
from litgraph.ingest.embeddings import embed_texts, paper_embedding_text
from litgraph.ingest.kaggle_source import iter_kaggle_papers
from litgraph.ingest.pubmed_baseline_source import iter_pubmed_baseline_papers
from litgraph.ingest.pubmed_source import fetch_historical_papers as fetch_historical_pubmed_papers
from litgraph.ingest.pubmed_source import fetch_new_papers as fetch_new_pubmed_papers
from litgraph.ingest.pubmed_source import get_checkpoint as get_pubmed_checkpoint
from litgraph.ingest.pubmed_source import set_checkpoint as set_pubmed_checkpoint
from litgraph.ingest.semantic_scholar import SemanticScholarClient
from litgraph.models import Paper
from litgraph.run_log import log_run

console = Console()


def _progress(*, determinate: bool = True) -> Progress:
    columns = [SpinnerColumn(), TextColumn("[progress.description]{task.description}")]
    if determinate:
        columns.append(BarColumn())
    columns += [MofNCompleteColumn(), TimeElapsedColumn()]
    return Progress(*columns, console=console)


_FIND_UNENRICHED = """
MATCH (p:Paper)
WHERE p.is_stub = false AND p.enriched_at IS NULL
  AND (p.arxiv_id IS NOT NULL OR p.pmid IS NOT NULL)
RETURN p.id AS id, p.arxiv_id AS arxiv_id, p.pmid AS pmid
LIMIT $limit
"""

_FIND_MISSING_EMBEDDINGS = """
MATCH (p:Paper)
WHERE p.is_stub = false AND p.embedding IS NULL
RETURN p.id AS id, p.title AS title, p.abstract AS abstract
LIMIT $limit
"""


def _start_of_this_week() -> datetime:
    """Monday 00:00 UTC of the current week."""
    today_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today_utc - timedelta(days=today_utc.weekday())


def _embed_and_upsert(papers: list[Paper]) -> None:
    if not papers:
        return
    texts = [paper_embedding_text(p.title, p.abstract) for p in papers]
    try:
        vectors = embed_texts(texts)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        # Upsert without embeddings to avoid losing this whole batch to a 
        # transient embedding-service outage.
        # scripts/backfill_embeddings.py finds and re-embeds any Paper with embedding IS
        # NULL later, so this is recoverable rather than a silent permanent gap.
        console.log(f"embed: service unavailable after retries, upserting {len(papers)} papers without embeddings ({exc})")
        upsert_papers(papers)
        return
    now = datetime.now()
    for paper, vector in zip(papers, vectors, strict=True):
        paper.embedding = vector
        paper.embedded_at = now
    upsert_papers(papers)


def run_backload(
    path: str | Path,
    categories: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
    batch_size: int = 200,
) -> int:
    """Stream the Kaggle snapshot, embed, and upsert matching papers. Returns count ingested."""
    started_at = datetime.now()
    batch: list[Paper] = []
    total = 0
    earliest: date | None = None
    latest: date | None = None

    with _progress(determinate=limit is not None) as progress:
        task = progress.add_task("Backloading papers", total=limit)
        for paper in iter_kaggle_papers(
            path, categories=categories, start_date=start_date, end_date=end_date, limit=limit
        ):
            batch.append(paper)
            published = paper.published_date
            if published is not None:
                if earliest is None or published < earliest:
                    earliest = published
                if latest is None or published > latest:
                    latest = published
            if len(batch) >= batch_size:
                _embed_and_upsert(batch)
                total += len(batch)
                progress.update(task, completed=total)
                batch = []
        if batch:
            _embed_and_upsert(batch)
            total += len(batch)
            progress.update(task, completed=total)

    console.log(f"backload: done, {total} papers upserted, batch spans {earliest} to {latest}")
    log_run(
        "backload",
        started_at,
        datetime.now(),
        total,
        categories=categories,
        requested_start_date=start_date.isoformat() if start_date else None,
        requested_end_date=end_date.isoformat() if end_date else None,
        earliest_published=earliest.isoformat() if earliest else None,
        latest_published=latest.isoformat() if latest else None,
        limit=limit,
    )
    return total


def run_daily_fetch(categories: list[str], batch_size: int = 200) -> int:
    """Fetch new papers since the last checkpoint, embed, and upsert. Returns count ingested."""
    started_at = datetime.now()
    checkpoint = get_checkpoint()
    since = checkpoint or _start_of_this_week()
    if checkpoint is None:
        console.log(f"fetch-daily: no checkpoint found, defaulting to start of this week ({since.isoformat()})")
    else:
        console.log(f"fetch-daily: last checkpoint = {since}")

    batch: list[Paper] = []
    total = 0
    newest_seen: datetime | None = None

    with _progress(determinate=False) as progress:
        task = progress.add_task("Fetching new papers", total=None)
        for paper in fetch_new_papers(categories, since=since):
            batch.append(paper)
            published = paper.published_date
            if published is not None:
                published_dt = datetime.combine(published, datetime.min.time())
                if newest_seen is None or published_dt > newest_seen:
                    newest_seen = published_dt
            if len(batch) >= batch_size:
                _embed_and_upsert(batch)
                total += len(batch)
                progress.update(task, completed=total)
                batch = []

        if batch:
            _embed_and_upsert(batch)
            total += len(batch)
            progress.update(task, completed=total)

    if newest_seen is not None:
        set_checkpoint(newest_seen)
    console.log(f"fetch-daily: done, {total} new papers upserted")
    log_run(
        "fetch-daily",
        started_at,
        datetime.now(),
        total,
        categories=categories,
        since_checkpoint=checkpoint.isoformat() if checkpoint else None,
        newest_seen=newest_seen.isoformat() if newest_seen else None,
    )
    return total


def run_backload_pubmed(
    dir_or_glob: str | Path,
    mesh_terms: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
    batch_size: int = 200,
) -> int:
    """Stream NCBI's PubMed baseline files, embed, and upsert matching papers. Returns count ingested."""
    started_at = datetime.now()
    batch: list[Paper] = []
    total = 0
    earliest: date | None = None
    latest: date | None = None

    with _progress(determinate=limit is not None) as progress:
        task = progress.add_task("Backloading PubMed papers", total=limit)
        for paper in iter_pubmed_baseline_papers(
            dir_or_glob, mesh_terms=mesh_terms, start_date=start_date, end_date=end_date, limit=limit
        ):
            batch.append(paper)
            published = paper.published_date
            if published is not None:
                if earliest is None or published < earliest:
                    earliest = published
                if latest is None or published > latest:
                    latest = published
            if len(batch) >= batch_size:
                _embed_and_upsert(batch)
                total += len(batch)
                progress.update(task, completed=total)
                batch = []
        if batch:
            _embed_and_upsert(batch)
            total += len(batch)
            progress.update(task, completed=total)

    console.log(f"backload-pubmed: done, {total} papers upserted, batch spans {earliest} to {latest}")
    log_run(
        "backload-pubmed",
        started_at,
        datetime.now(),
        total,
        mesh_terms=mesh_terms,
        requested_start_date=start_date.isoformat() if start_date else None,
        requested_end_date=end_date.isoformat() if end_date else None,
        earliest_published=earliest.isoformat() if earliest else None,
        latest_published=latest.isoformat() if latest else None,
        limit=limit,
    )
    return total


def run_backload_pubmed_api(
    mesh_terms: str,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 200,
) -> int:
    """Historical backload of PubMed papers matching ``mesh_terms``, fetched entirely via
    NCBI E-utilities (no bulk baseline files) -- NCBI filters server-side by the query,
    so this only transfers matching records rather than the full corpus. Returns count
    ingested."""
    started_at = datetime.now()
    batch: list[Paper] = []
    total = 0
    earliest: date | None = None
    latest: date | None = None

    with _progress(determinate=False) as progress:
        task = progress.add_task("Backloading PubMed papers via API", total=None)
        for paper in fetch_historical_pubmed_papers(
            mesh_terms, start_date=start_date, end_date=end_date, batch_size=batch_size
        ):
            batch.append(paper)
            published = paper.published_date
            if published is not None:
                if earliest is None or published < earliest:
                    earliest = published
                if latest is None or published > latest:
                    latest = published
            if len(batch) >= batch_size:
                _embed_and_upsert(batch)
                total += len(batch)
                progress.update(task, completed=total)
                batch = []
        if batch:
            _embed_and_upsert(batch)
            total += len(batch)
            progress.update(task, completed=total)

    console.log(f"backload-pubmed-api: done, {total} papers upserted, batch spans {earliest} to {latest}")
    log_run(
        "backload-pubmed-api",
        started_at,
        datetime.now(),
        total,
        mesh_terms=mesh_terms,
        requested_start_date=start_date.isoformat() if start_date else None,
        requested_end_date=end_date.isoformat() if end_date else None,
        earliest_published=earliest.isoformat() if earliest else None,
        latest_published=latest.isoformat() if latest else None,
    )
    return total


def run_daily_fetch_pubmed(mesh_terms: str, batch_size: int = 200) -> int:
    """Fetch new PubMed papers since the last checkpoint, embed, and upsert. Returns count ingested."""
    started_at = datetime.now()
    checkpoint = get_pubmed_checkpoint()
    since = checkpoint or _start_of_this_week()
    if checkpoint is None:
        console.log(
            f"fetch-daily-pubmed: no checkpoint found, defaulting to start of this week ({since.isoformat()})"
        )
    else:
        console.log(f"fetch-daily-pubmed: last checkpoint = {since}")

    batch: list[Paper] = []
    total = 0
    newest_seen: datetime | None = None

    with _progress(determinate=False) as progress:
        task = progress.add_task("Fetching new PubMed papers", total=None)
        for paper in fetch_new_pubmed_papers(mesh_terms, since=since):
            batch.append(paper)
            published = paper.published_date
            if published is not None:
                published_dt = datetime.combine(published, datetime.min.time())
                if newest_seen is None or published_dt > newest_seen:
                    newest_seen = published_dt
            if len(batch) >= batch_size:
                _embed_and_upsert(batch)
                total += len(batch)
                progress.update(task, completed=total)
                batch = []

        if batch:
            _embed_and_upsert(batch)
            total += len(batch)
            progress.update(task, completed=total)

    if newest_seen is not None:
        set_pubmed_checkpoint(newest_seen)
    console.log(f"fetch-daily-pubmed: done, {total} new papers upserted")
    log_run(
        "fetch-daily-pubmed",
        started_at,
        datetime.now(),
        total,
        mesh_terms=mesh_terms,
        since_checkpoint=checkpoint.isoformat() if checkpoint else None,
        newest_seen=newest_seen.isoformat() if newest_seen else None,
    )
    return total


def run_enrichment(limit: int = 500) -> int:
    """Enrich up to ``limit`` not-yet-enriched papers with Semantic Scholar citation data."""
    started_at = datetime.now()
    rows = run_read(_FIND_UNENRICHED, limit=limit)
    if not rows:
        console.log("enrich: nothing to do")
        log_run("enrich", started_at, datetime.now(), 0, limit=limit, skipped=0)
        return 0

    arxiv_pairs = [(r["id"], r["arxiv_id"]) for r in rows if r["arxiv_id"]]
    pmid_pairs = [(r["id"], r["pmid"]) for r in rows if r["arxiv_id"] is None and r["pmid"]]

    batch_size = get_settings().semantic_scholar_batch_size
    enriched_total = 0
    not_found_total = 0
    total = len(arxiv_pairs) + len(pmid_pairs)
    with SemanticScholarClient() as client, _progress() as progress:
        task = progress.add_task("Enriching papers", total=total)
        for pairs, id_prefix in ((arxiv_pairs, "ARXIV"), (pmid_pairs, "PMID")):
            for batch in chunked(pairs, batch_size):
                try:
                    results = client.enrich(batch, id_prefix=id_prefix)
                except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                    console.log(f"enrich: batch of {len(batch)} failed after retries, skipping ({exc})")
                    progress.update(task, advance=len(batch))
                    continue
                # Every requested paper gets a result now (client.enrich() no longer drops
                # not-found papers) -- apply_enrichment() stamps enriched_at on all of them,
                # found or not, so a "not found in S2" paper doesn't keep reappearing at
                # the front of _FIND_UNENRICHED's LIMIT window on every future run.
                apply_enrichment(results)
                enriched_total += len(results)
                not_found_total += sum(1 for r in results if r.s2_paper_id is None)
                progress.update(task, advance=len(batch))

    console.log(
        f"enrich: processed {enriched_total}/{total} papers"
        + (f" ({not_found_total} not found in Semantic Scholar)" if not_found_total else "")
    )
    log_run("enrich", started_at, datetime.now(), enriched_total, limit=limit, not_found=not_found_total)
    return enriched_total


def run_backfill_embeddings(batch_size: int = 200) -> int:
    """Embed any fully-ingested paper that's missing an embedding -- e.g. ones upserted
    during an embedding-service outage by _embed_and_upsert()'s fallback path. Uses the
    title/abstract already stored in the graph rather than re-fetching from an external
    API, so it works regardless of source (arxiv, kaggle, pubmed, ...). Returns count
    embedded."""
    started_at = datetime.now()
    total = 0
    with _progress(determinate=False) as progress:
        task = progress.add_task("Backfilling embeddings", total=None)
        while True:
            rows = run_read(_FIND_MISSING_EMBEDDINGS, limit=batch_size)
            if not rows:
                break
            texts = [paper_embedding_text(r["title"] or "", r["abstract"] or "") for r in rows]
            try:
                vectors = embed_texts(texts)
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                console.log(f"backfill-embeddings: service unavailable after retries, stopping ({exc})")
                break
            now = datetime.now()
            set_paper_embeddings([(r["id"], v) for r, v in zip(rows, vectors, strict=True)], now)
            total += len(rows)
            progress.update(task, completed=total)

    console.log(f"backfill-embeddings: done, {total} papers embedded")
    log_run("backfill-embeddings", started_at, datetime.now(), total, batch_size=batch_size)
    return total
