"""Shared PubmedArticle XML -> Paper mapping, used by both pubmed_source.py (E-utilities
efetch responses) and pubmed_baseline_source.py (NCBI baseline/update bulk files) since
both are the same PubMed XML schema."""

import io
from datetime import date

from lxml import etree

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _text(el, path: str) -> str | None:
    node = el.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _join_text(el) -> str:
    """Join all text content of an element and its children (handles inline markup like <i>)."""
    return "".join(el.itertext()).strip()


def _parse_pub_date(article_el) -> date | None:
    pub_date_el = article_el.find("Journal/JournalIssue/PubDate")
    if pub_date_el is None:
        return None
    year = _text(pub_date_el, "Year")
    if year is None:
        medline_date = _text(pub_date_el, "MedlineDate")
        if medline_date and medline_date[:4].isdigit():
            year = medline_date[:4]
    if year is None or not year.isdigit():
        return None
    month_text = _text(pub_date_el, "Month")
    month = _MONTHS.get(month_text, None) if month_text else None
    if month is None and month_text and month_text.isdigit():
        month = int(month_text)
    day_text = _text(pub_date_el, "Day")
    day = int(day_text) if day_text and day_text.isdigit() else None
    try:
        return date(int(year), month or 1, day or 1)
    except ValueError:
        return date(int(year), 1, 1)


def _parse_ymd(el, base_path: str) -> date | None:
    year = _text(el, f"{base_path}/Year")
    month = _text(el, f"{base_path}/Month")
    day = _text(el, f"{base_path}/Day")
    if not (year and year.isdigit() and month and month.isdigit() and day and day.isdigit()):
        return None
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _mesh_headings(citation_el) -> tuple[list[str], str | None]:
    headings = []
    major_headings = []
    for heading_el in citation_el.findall("MeshHeadingList/MeshHeading"):
        descriptor = heading_el.find("DescriptorName")
        if descriptor is None or not descriptor.text:
            continue
        name = descriptor.text.strip()
        headings.append(name)
        if descriptor.get("MajorTopicYN") == "Y":
            major_headings.append(name)
    primary = major_headings[0] if major_headings else (headings[0] if headings else None)
    return headings, primary


def _doi(article_el) -> str | None:
    for article_id in article_el.findall("ELocationID"):
        if article_id.get("EIdType") == "doi" and article_id.text:
            return article_id.text.strip()
    return None


def _pubmed_data_doi(pubmed_article_el) -> str | None:
    for article_id in pubmed_article_el.findall("PubmedData/ArticleIdList/ArticleId"):
        if article_id.get("IdType") == "doi" and article_id.text:
            return article_id.text.strip()
    return None


def _journal_ref(article_el) -> str | None:
    journal_el = article_el.find("Journal")
    if journal_el is None:
        return None
    name = _text(journal_el, "ISOAbbreviation") or _text(journal_el, "Title")
    if not name:
        return None
    issue_el = journal_el.find("JournalIssue")
    year = _text(issue_el, "PubDate/Year") if issue_el is not None else None
    volume = _text(issue_el, "Volume") if issue_el is not None else None
    issue = _text(issue_el, "Issue") if issue_el is not None else None
    pages = _text(article_el, "Pagination/MedlinePgn")

    ref = name
    if year:
        ref += f". {year}"
    if volume:
        ref += f";{volume}"
    if issue:
        ref += f"({issue})"
    if pages:
        ref += f":{pages}"
    return ref


def parse_pubmed_article(pubmed_article_el) -> dict:
    """Extract the fields needed to build a ``Paper`` from a single <PubmedArticle> element.

    Returns a plain dict rather than a ``Paper`` so callers can attach ``source`` themselves.
    """
    citation_el = pubmed_article_el.find("MedlineCitation")
    article_el = citation_el.find("Article")

    pmid = _text(citation_el, "PMID")

    title_el = article_el.find("ArticleTitle")
    title = _join_text(title_el) if title_el is not None else ""

    abstract_parts = [_join_text(a) for a in article_el.findall("Abstract/AbstractText")]
    abstract = " ".join(part for part in abstract_parts if part)

    authors = []
    for author_el in article_el.findall("AuthorList/Author"):
        last = _text(author_el, "LastName")
        fore = _text(author_el, "ForeName")
        name = " ".join(part for part in (fore, last) if part).strip()
        if name:
            authors.append(name)
        elif _text(author_el, "CollectiveName"):
            authors.append(_text(author_el, "CollectiveName"))

    categories, primary_category = _mesh_headings(citation_el)

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "categories": categories,
        "primary_category": primary_category,
        "published_date": _parse_pub_date(article_el),
        "updated_date": _parse_ymd(citation_el, "DateRevised"),
        "doi": _doi(article_el) or _pubmed_data_doi(pubmed_article_el),
        "journal_ref": _journal_ref(article_el),
        "comments": None,
    }


def iter_pubmed_articles(source):
    """Stream <PubmedArticle> elements out of a PubmedArticleSet XML document, freeing
    each element's memory once processed (important for multi-GB baseline files).

    ``source`` may be raw bytes (small documents, e.g. an efetch response) or a
    file-like object opened for binary reading (e.g. a gzip-decompressed baseline file).
    """
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    context = etree.iterparse(source, tag="PubmedArticle")
    for _, element in context:
        yield element
        element.clear()
        while element.getprevious() is not None:
            del element.getparent()[0]
