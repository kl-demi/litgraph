import json
from datetime import date

from arxiv_graphdb.ingest.kaggle_source import iter_kaggle_papers

RECORD_CS = {
    "id": "2101.00001",
    "title": "A Great Paper\nAbout Things",
    "abstract": "This is the\nabstract.",
    "categories": "cs.CL cs.LG",
    "authors_parsed": [["Doe", "Jane", ""], ["Smith", "John", ""]],
    "versions": [{"created": "Mon, 4 Jan 2021 12:00:00 GMT"}],
    "update_date": "2021-02-01",
    "doi": None,
    "journal-ref": None,
    "comments": "10 pages",
}

RECORD_PHYSICS = {
    **RECORD_CS,
    "id": "2101.00002",
    "categories": "physics.gen-ph",
}


def _write_jsonl(tmp_path, records):
    path = tmp_path / "snapshot.json"
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def test_parses_basic_fields(tmp_path):
    path = _write_jsonl(tmp_path, [RECORD_CS])
    papers = list(iter_kaggle_papers(path))
    assert len(papers) == 1
    paper = papers[0]
    assert paper.arxiv_id == "2101.00001"
    assert paper.title == "A Great Paper About Things"
    assert paper.abstract == "This is the abstract."
    assert paper.authors == ["Jane Doe", "John Smith"]
    assert paper.categories == ["cs.CL", "cs.LG"]
    assert paper.primary_category == "cs.CL"
    assert paper.published_date == date(2021, 1, 4)
    assert paper.updated_date == date(2021, 2, 1)
    assert paper.source == "kaggle"


def test_filters_by_category_prefix(tmp_path):
    path = _write_jsonl(tmp_path, [RECORD_CS, RECORD_PHYSICS])
    papers = list(iter_kaggle_papers(path, categories=["cs"]))
    assert [p.arxiv_id for p in papers] == ["2101.00001"]


def test_filters_by_date_range(tmp_path):
    path = _write_jsonl(tmp_path, [RECORD_CS])
    assert list(iter_kaggle_papers(path, start_date=date(2021, 1, 5))) == []
    assert len(list(iter_kaggle_papers(path, start_date=date(2021, 1, 1)))) == 1
    assert list(iter_kaggle_papers(path, end_date=date(2021, 1, 1))) == []


def test_respects_limit(tmp_path):
    records = [{**RECORD_CS, "id": f"2101.0000{i}"} for i in range(5)]
    path = _write_jsonl(tmp_path, records)
    papers = list(iter_kaggle_papers(path, limit=2))
    assert len(papers) == 2
