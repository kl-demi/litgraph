from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from litgraph.config import get_settings
from litgraph.db.neo4j_client import chunked, run_read
from litgraph.graph.upsert import apply_enrichment, set_paper_embeddings, upsert_papers
from litgraph.ingest.arxiv_source import fetch_new_papers
from litgraph.ingest.checkpoint import get_checkpoint, set_checkpoint
from litgraph.ingest.embeddings import embed_texts, paper_embedding_text
from litgraph.ingest.kaggle_source import iter_kaggle_papers
from litgraph.ingest.pubmed_baseline_source import iter_pubmed_baseline_papers
from litgraph.ingest.pubmed_source import fetch_historical_papers as fetch_historical_pubmed_papers
from litgraph.ingest.pubmed_source import fetch_new_papers as fetch_new_pubmed_papers
from litgraph.ingest.semantic_scholar import SemanticScholarClient
from litgraph.models import Paper
from litgraph.run_log import log_run

console = Console()

# Job names for the two forward-cursor checkpoints (see ingest/checkpoint.py).
_ARXIV_DAILY_JOB = "arxiv_daily"
_PUBMED_DAILY_JOB = "pubmed_daily"


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


def _consume(
    papers: Iterator[Paper],
    *,
    batch_size: int,
    label: str,
    determinate: bool = False,
    total: int | None = None,
    date_filter: Callable[[date], bool] = lambda _: True,
) -> tuple[int, date | None, date | None]:
    """Batch-embed-and-upsert an iterator of Paper, tracking count and the
    earliest/latest ``published_date`` seen.

    ``date_filter`` controls which dates count toward earliest/latest without
    affecting whether a paper is ingested -- PubMed's daily fetch uses this to
    keep a future-dated PubDate from advancing its checkpoint while still
    upserting the paper (see docs/known_bugs.md).

    Returns (total ingested, earliest published_date, latest published_date).
    """
    batch: list[Paper] = []
    total_count = 0
    earliest: date | None = None
    latest: date | None = None

    with _progress(determinate=determinate) as progress:
        task = progress.add_task(label, total=total)

        def flush() -> None:
            nonlocal batch, total_count
            if not batch:
                return
            _embed_and_upsert(batch)
            total_count += len(batch)
            progress.update(task, completed=total_count)
            batch = []

        for paper in papers:
            batch.append(paper)
            published = paper.published_date
            if published is not None and date_filter(published):
                if earliest is None or published < earliest:
                    earliest = published
                if latest is None or published > latest:
                    latest = published
            if len(batch) >= batch_size:
                flush()
        flush()

    return total_count, earliest, latest


def _embed_and_upsert(papers: list[Paper]) -> None:
    if not papers:
        return
    texts = [paper_embedding_text(p.title, p.abstract) for p in papers]
    try:
        vectors = embed_texts(texts)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        # Upsert without embeddings to avoid losing this whole batch to a 
        # transient embedding-service outage.
        # scripts/backfill_embeddings.py can find and re-embed any Paper without
        # an embedding later.
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
    papers = iter_kaggle_papers(path, categories=categories, start_date=start_date, end_date=end_date, limit=limit)
    total, earliest, latest = _consume(
        papers, batch_size=batch_size, label="Backloading papers", determinate=limit is not None, total=limit
    )

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
    checkpoint = get_checkpoint(_ARXIV_DAILY_JOB)
    since = checkpoint or _start_of_this_week()
    if checkpoint is None:
        console.log(f"fetch-daily: no checkpoint found, defaulting to start of this week ({since.isoformat()})")
    else:
        console.log(f"fetch-daily: last checkpoint = {since}")

    total, _earliest, latest = _consume(
        fetch_new_papers(categories, since=since), batch_size=batch_size, label="Fetching new papers"
    )

    newest_seen = datetime.combine(latest, datetime.min.time()) if latest else None
    if newest_seen is not None:
        set_checkpoint(newest_seen, _ARXIV_DAILY_JOB)
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
    papers = iter_pubmed_baseline_papers(
        dir_or_glob, mesh_terms=mesh_terms, start_date=start_date, end_date=end_date, limit=limit
    )
    total, earliest, latest = _consume(
        papers, batch_size=batch_size, label="Backloading PubMed papers", determinate=limit is not None, total=limit
    )

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
    limit: int | None = None,
) -> int:
    """Historical backload of PubMed papers matching ``mesh_terms``, fetched entirely via
    NCBI E-utilities (no bulk baseline files). This is to avoid downloading the full corpus
    which is 40+GB compressed/ 120+ GB uncompressed XML.

    Walks newest-published-first and checkpoints the oldest ``published_date`` reached
    after every batch, keyed by ``mesh_terms`` -- an interrupted run resumes from there
    on the next invocation instead of re-walking from "now". Pass an explicit ``end_date`` 
    to bypass the checkpoint and pin a specific historical slice instead. 
    
    ``limit`` stops this run cleanly after that many papers, to bound a single invocation's 
    cost -- the next call resumes from the checkpoint same as if it had been killed. Returns
    count ingested.
    """
    started_at = datetime.now()
    requested_end_date = end_date
    checkpoint_job = f"pubmed_backload_api:{mesh_terms}"
    resumed_from: date | None = None
    if end_date is None:
        checkpoint = get_checkpoint(checkpoint_job)
        if checkpoint is not None:
            resumed_from = checkpoint.date()
            end_date = resumed_from
            console.log(f"backload-pubmed-api: resuming, continuing backward from checkpoint {resumed_from}")

    batch: list[Paper] = []
    total = 0
    earliest: date | None = None
    latest: date | None = None
    unreachable = 0
    windows_done = 0

    with _progress(determinate=False) as progress:
        task = progress.add_task("Backloading PubMed papers via API", total=None)

        def flush() -> None:
            nonlocal batch, total
            if not batch:
                return
            _embed_and_upsert(batch)
            total += len(batch)
            progress.update(task, completed=total)
            batch = []

        def on_window_complete(resume_from: date, skipped: int) -> None:
            """Record the resume boundary once a date window is fully ingested.

            Flushes first: writing the checkpoint ahead of the upsert would permanently
            skip whatever is still buffered if the run died in between.
            """
            nonlocal unreachable, windows_done
            flush()
            set_checkpoint(datetime.combine(resume_from, datetime.min.time()), checkpoint_job)
            windows_done += 1
            if skipped:
                unreachable += skipped
                console.log(
                    f"[yellow]backload-pubmed-api: {skipped} records in the single-day window "
                    f"{resume_from + timedelta(days=1)} exceed efetch's paging limit and were "
                    f"skipped -- that day cannot be split any further[/yellow]"
                )

        for paper in fetch_historical_pubmed_papers(
            mesh_terms,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
            limit=limit,
            on_window_complete=on_window_complete,
        ):
            batch.append(paper)
            published = paper.published_date
            if published is not None:
                if earliest is None or published < earliest:
                    earliest = published
                if latest is None or published > latest:
                    latest = published
            if len(batch) >= batch_size:
                flush()
        flush()

    console.log(
        f"backload-pubmed-api: done, {total} papers upserted across {windows_done} date "
        f"window(s), batch spans {earliest} to {latest}"
    )
    if unreachable:
        console.log(f"[yellow]backload-pubmed-api: {unreachable} records total were unreachable[/yellow]")
    log_run(
        "backload-pubmed-api",
        started_at,
        datetime.now(),
        total,
        mesh_terms=mesh_terms,
        requested_start_date=start_date.isoformat() if start_date else None,
        requested_end_date=requested_end_date.isoformat() if requested_end_date else None,
        resumed_from_checkpoint=resumed_from.isoformat() if resumed_from else None,
        limit=limit,
        earliest_published=earliest.isoformat() if earliest else None,
        latest_published=latest.isoformat() if latest else None,
        date_windows_completed=windows_done,
        unreachable_records=unreachable,
    )
    return total


def run_daily_fetch_pubmed(mesh_terms: str, batch_size: int = 200) -> int:
    """Fetch new PubMed papers since the last checkpoint, embed, and upsert. Returns count ingested."""
    started_at = datetime.now()
    checkpoint = get_checkpoint(_PUBMED_DAILY_JOB)
    since = checkpoint or _start_of_this_week()
    if checkpoint is None:
        console.log(
            f"fetch-daily-pubmed: no checkpoint found, defaulting to start of this week ({since.isoformat()})"
        )
    else:
        console.log(f"fetch-daily-pubmed: last checkpoint = {since}")

    today = datetime.now(UTC).date()
    total, _earliest, latest = _consume(
        fetch_new_pubmed_papers(mesh_terms, since=since),
        batch_size=batch_size,
        label="Fetching new PubMed papers",
        # PubMed's PubDate is the journal issue's cover date, which can be dated
        # months ahead and falsely push the checkpoint into the future.
        date_filter=lambda published: published <= today,
    )

    newest_seen = datetime.combine(latest, datetime.min.time()) if latest else None
    if newest_seen is not None:
        set_checkpoint(newest_seen, _PUBMED_DAILY_JOB)
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
    """Embed any ingested paper that's missing an embedding. Uses the title/abstract 
    already stored in the graph rather than re-fetching from an external API, so it works 
    regardless of source (arxiv, kaggle, pubmed, ...). Returns count embedded.
    """
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
