import pytest
from pydantic import ValidationError

from litgraph.models import CitationStub, Paper


def test_paper_id_prefers_arxiv_id():
    paper = Paper(arxiv_id="2101.00001", s2_paper_id="s2-1", title="T")
    assert paper.id == "2101.00001"


def test_paper_id_falls_back_to_s2():
    paper = Paper(s2_paper_id="s2-1", title="T")
    assert paper.id == "s2:s2-1"


def test_paper_requires_an_identifier():
    with pytest.raises(ValidationError):
        Paper(title="T")


def test_citation_stub_requires_an_identifier():
    with pytest.raises(ValueError):
        CitationStub(title="T").id
