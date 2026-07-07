import email.utils
import gzip
import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

from arxiv_graphdb.models import Paper


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def _parse_created_date(created: str | None) -> date | None:
    if not created:
        return None
    try:
        return email.utils.parsedate_to_datetime(created).date()
    except (TypeError, ValueError):
        return None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _record_to_paper(record: dict) -> Paper:
    categories = (record.get("categories") or "").split()
    authors = [
        " ".join(part for part in (a[1], a[0]) if part).strip()
        for a in (record.get("authors_parsed") or [])
        if a
    ]
    versions = record.get("versions") or []
    published_date = _parse_created_date(versions[0].get("created")) if versions else None

    return Paper(
        arxiv_id=record.get("id"),
        title=(record.get("title") or "").replace("\n", " ").strip(),
        abstract=(record.get("abstract") or "").replace("\n", " ").strip(),
        authors=authors,
        categories=categories,
        primary_category=categories[0] if categories else None,
        published_date=published_date,
        updated_date=_parse_iso_date(record.get("update_date")),
        doi=record.get("doi") or None,
        journal_ref=record.get("journal-ref") or None,
        comments=record.get("comments") or None,
        source="kaggle",
    )


def iter_kaggle_papers(
    path: str | Path,
    categories: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
) -> Iterator[Paper]:
    """Stream-parse the Kaggle arXiv metadata snapshot (JSON-lines), filtering as we go.

    ``categories`` matches if any of the paper's categories share a prefix with any of the
    given codes (so "cs" matches "cs.CL", "cs.LG", ...). Date filtering uses the paper's
    original submission date (first version).
    """
    category_prefixes = tuple(categories) if categories else None
    yielded = 0

    with _open(Path(path)) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            paper_categories = (record.get("categories") or "").split()

            if category_prefixes and not any(
                cat == prefix or cat.startswith(prefix + ".")
                for cat in paper_categories
                for prefix in category_prefixes
            ):
                continue

            paper = _record_to_paper(record)

            if start_date and (paper.published_date is None or paper.published_date < start_date):
                continue
            if end_date and (paper.published_date is None or paper.published_date > end_date):
                continue

            yield paper
            yielded += 1
            if limit is not None and yielded >= limit:
                return
