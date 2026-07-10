import gzip
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from litgraph.ingest._pubmed_xml import iter_pubmed_articles, parse_pubmed_article
from litgraph.models import Paper


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return open(path, "rb")


def _iter_files(dir_or_glob: str | Path) -> Iterator[Path]:
    path = Path(dir_or_glob)
    if path.is_dir():
        yield from sorted(path.glob("pubmed*.xml.gz"))
        yield from sorted(path.glob("pubmed*.xml"))
    elif path.is_file():
        yield path
    else:
        yield from sorted(Path().glob(str(dir_or_glob)))


def _fields_to_paper(fields: dict) -> Paper:
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
        source="pubmed_baseline",
    )


def iter_pubmed_baseline_papers(
    dir_or_glob: str | Path,
    mesh_terms: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
) -> Iterator[Paper]:
    """Stream-parse NCBI's PubMed baseline/update bulk XML files, filtering as we go.

    ``mesh_terms`` matches if any of the paper's MeSH headings equal one of the given terms
    (case-insensitive). Date filtering uses the paper's published date.
    """
    mesh_term_set = {t.lower() for t in mesh_terms} if mesh_terms else None
    yielded = 0

    for path in _iter_files(dir_or_glob):
        with _open(path) as f:
            for article_el in iter_pubmed_articles(f):
                fields = parse_pubmed_article(article_el)
                if not fields["pmid"]:
                    continue

                if mesh_term_set and not any(
                    cat.lower() in mesh_term_set for cat in fields["categories"]
                ):
                    continue

                published = fields["published_date"]
                if start_date and (published is None or published < start_date):
                    continue
                if end_date and (published is None or published > end_date):
                    continue

                yield _fields_to_paper(fields)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
