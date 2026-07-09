from litgraph.graph import upsert
from litgraph.models import CitationStub, EnrichmentResult, Paper


def test_upsert_papers_builds_expected_params(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    paper = Paper(arxiv_id="2101.00001", title="Title", abstract="Abstract", authors=["Jane Doe"], categories=["cs.CL"])

    upsert.upsert_papers([paper])

    calls = mock_run_write.call_args_list
    assert len(calls) == 3
    paper_query, category_query, author_query = (c.args[0] for c in calls)
    assert "MERGE (paper:Paper {id: p.id})" in paper_query
    assert "MERGE (c:Category {code: cat})" in category_query
    assert "MERGE (a:Author {name: authorName})" in author_query
    for call in calls:
        papers_param = call.kwargs["papers"]
        assert papers_param[0]["id"] == "2101.00001"
        assert papers_param[0]["authors"] == ["Jane Doe"]


def test_upsert_papers_noop_on_empty(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    upsert.upsert_papers([])
    mock_run_write.assert_not_called()


def test_upsert_paper_stubs_dedupes(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    stubs = [
        CitationStub(arxiv_id="2001.00001", title="A"),
        CitationStub(arxiv_id="2001.00001", title="A duplicate"),
        CitationStub(s2_paper_id="s2-9", title="B"),
    ]

    upsert.upsert_paper_stubs(stubs)

    kwargs = mock_run_write.call_args.kwargs
    ids = {s["id"] for s in kwargs["stubs"]}
    assert ids == {"2001.00001", "s2:s2-9"}


def test_apply_enrichment_builds_edges_and_stubs(mocker):
    """
    Verifies that apply_enrichment() correctly processes citation data from
    Semantic Scholar and calls the database write function with the right params
    """
    mock_run_write = mocker.patch.object(upsert, "run_write")
    result = EnrichmentResult(
        arxiv_id="2101.00001",
        s2_paper_id="s2-1",
        citation_count=3,
        references=[CitationStub(arxiv_id="2001.00001", title="Ref")],
        citations=[CitationStub(s2_paper_id="s2-2", title="Citer")],
    )

    upsert.apply_enrichment([result])

    calls = mock_run_write.call_args_list
    stub_call = next(c for c in calls if "stubs" in c.kwargs)
    edge_call = next(c for c in calls if "edges" in c.kwargs)
    enrichment_call = next(c for c in calls if "results" in c.kwargs)

    # Check that stubs are created for both papers "2001.00001" and "s2-2"
    stub_ids = {s["id"] for s in stub_call.kwargs["stubs"]}
    assert stub_ids == {"2001.00001", "s2:s2-2"}

    # Check that edges exist: 2101 -> 2001 and s2-2 -> 2101
    edges = {(e["citing_id"], e["cited_id"]) for e in edge_call.kwargs["edges"]}
    assert ("2101.00001", "2001.00001") in edges
    assert ("s2:s2-2", "2101.00001") in edges

    # Check that enrichment data (citation count = 3) is persisted
    assert enrichment_call.kwargs["results"][0]["citation_count"] == 3


def test_apply_enrichment_noop_on_empty(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    upsert.apply_enrichment([])
    mock_run_write.assert_not_called()
