from datetime import date, datetime
from pathlib import Path

from rich.console import Console

from arxiv_graphdb.db.neo4j_client import run_read
from arxiv_graphdb.graph.upsert import apply_enrichment, upsert_papers
from arxiv_graphdb.ingest.arxiv_source import fetch_new_papers, get_checkpoint, set_checkpoint
from arxiv_graphdb.ingest.embeddings import embed_texts, paper_embedding_text
from arxiv_graphdb.ingest.kaggle_source import iter_kaggle_papers
from arxiv_graphdb.ingest.semantic_scholar import SemanticScholarClient
from arxiv_graphdb.models import Paper

console = Console()

_FIND_UNENRICHED = """
MATCH (p:Paper)
WHERE p.is_stub = false AND p.arxiv_id IS NOT NULL AND p.enriched_at IS NULL
RETURN p.arxiv_id AS arxiv_id
LIMIT $limit
"""


def _embed_and_upsert(papers: list[Paper]) -> None:
    if not papers:
        return
    texts = [paper_embedding_text(p.title, p.abstract) for p in papers]
    vectors = embed_texts(texts)
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
    batch: list[Paper] = []
    total = 0
    for paper in iter_kaggle_papers(
        path, categories=categories, start_date=start_date, end_date=end_date, limit=limit
    ):
        batch.append(paper)
        if len(batch) >= batch_size:
            _embed_and_upsert(batch)
            total += len(batch)
            console.log(f"backload: upserted {total} papers so far")
            batch = []
    if batch:
        _embed_and_upsert(batch)
        total += len(batch)
    console.log(f"backload: done, {total} papers upserted")
    return total


def run_daily_fetch(categories: list[str], batch_size: int = 200) -> int:
    """Fetch new papers since the last checkpoint, embed, and upsert. Returns count ingested."""
    since = get_checkpoint()
    console.log(f"fetch-daily: last checkpoint = {since}")

    batch: list[Paper] = []
    total = 0
    newest_seen: datetime | None = None

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
            batch = []

    if batch:
        _embed_and_upsert(batch)
        total += len(batch)

    if newest_seen is not None:
        set_checkpoint(newest_seen)
    console.log(f"fetch-daily: done, {total} new papers upserted")
    return total


def run_enrichment(limit: int = 500) -> int:
    """Enrich up to ``limit`` not-yet-enriched papers with Semantic Scholar citation data."""
    rows = run_read(_FIND_UNENRICHED, limit=limit)
    arxiv_ids = [r["arxiv_id"] for r in rows]
    if not arxiv_ids:
        console.log("enrich: nothing to do")
        return 0

    with SemanticScholarClient() as client:
        results = client.enrich(arxiv_ids)

    apply_enrichment(results)
    console.log(f"enrich: enriched {len(results)}/{len(arxiv_ids)} papers")
    return len(results)
