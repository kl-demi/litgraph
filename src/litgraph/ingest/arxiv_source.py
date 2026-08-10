import re
from collections.abc import Iterator
from datetime import UTC, datetime

import arxiv

from litgraph.models import Paper, Source, arxiv_category

_VERSION_SUFFIX = re.compile(r"v\d+$")


def _strip_version(short_id: str) -> str:
    return _VERSION_SUFFIX.sub("", short_id)


def _result_to_paper(result: arxiv.Result) -> Paper:
    return Paper(
        arxiv_id=_strip_version(result.get_short_id()),
        title=result.title.replace("\n", " ").strip(),
        abstract=result.summary.replace("\n", " ").strip(),
        authors=[a.name for a in result.authors],
        categories=[arxiv_category(c) for c in result.categories],
        primary_category=arxiv_category(result.primary_category).code if result.primary_category else None,
        published_date=result.published.date() if result.published else None,
        updated_date=result.updated.date() if result.updated else None,
        doi=result.doi,
        journal_ref=result.journal_ref,
        comments=result.comment,
        source=Source.ARXIV,
        fetched_at=datetime.now(UTC),
    )


def fetch_new_papers(
    categories: list[str],
    since: datetime | None = None,
    max_results: int = 2000,
) -> Iterator[Paper]:
    """Fetch papers in the given categories, newest first, stopping once we reach ``since``.

    Intended to be called with the last checkpoint so re-running is a no-op once caught up.
    """
    query = " OR ".join(f"cat:{c}" for c in categories)
    client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    for result in client.results(search):
        if since is not None and result.published is not None:
            published = result.published
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            if published <= since:
                return
        yield _result_to_paper(result)
