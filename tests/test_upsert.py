from datetime import datetime

from litgraph.graph import upsert
from litgraph.models import CitationStub, EnrichmentResult, Paper


def _mock_run_write(mocker, **first_call_results):
    """Mock run_write so every call returns a delta row a stats-apply call can consume.

    Each upsert query now returns one row of stats deltas; `first_call_results` lets a
    test override specific delta keys (e.g. new_papers=1) while everything else
    defaults to 0/None, since the stats-apply call unpacks `**delta` as kwargs.
    """
    defaults = {
        "new_papers": 0,
        "upgraded_stubs": 0,
        "embedded_delta": 0,
        "batch_min_date": None,
        "batch_max_date": None,
        "new_categories": 0,
        "new_edges": 0,
        "new_authors": 0,
        "new_stubs": 0,
        "newly_enriched_count": 0,
    }
    row = {**defaults, **first_call_results}
    mock_run_write = mocker.patch.object(upsert, "run_write")
    mock_run_write.return_value = [row]
    return mock_run_write


def test_upsert_papers_builds_expected_params(mocker):
    mock_run_write = _mock_run_write(mocker)
    paper = Paper(arxiv_id="2101.00001", title="Title", abstract="Abstract", authors=["Jane Doe"], categories=["cs.CL"])

    upsert.upsert_papers([paper])

    calls = mock_run_write.call_args_list
    assert len(calls) == 6
    paper_query, paper_stats_query, category_query, category_stats_query, author_query, author_stats_query = (
        c.args[0] for c in calls
    )
    assert "MERGE (paper:Paper {id: p.id})" in paper_query
    assert "GraphStats" in paper_stats_query
    assert "MERGE (c:Category {code: cat})" in category_query
    assert "GraphStats" in category_stats_query
    assert "MERGE (a:Author {name: authorName})" in author_query
    assert "GraphStats" in author_stats_query

    for query_call in (calls[0], calls[2], calls[4]):
        papers_param = query_call.kwargs["papers"]
        assert papers_param[0]["id"] == "2101.00001"
        assert papers_param[0]["authors"] == ["Jane Doe"]


def test_upsert_papers_noop_on_empty(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    upsert.upsert_papers([])
    mock_run_write.assert_not_called()


def test_upsert_papers_threads_stats_delta_into_apply_call(mocker):
    """The delta row returned by the paper-upsert query must be passed straight
    through as params to the GraphStats-apply query, unchanged."""
    mock_run_write = _mock_run_write(mocker, new_papers=1, upgraded_stubs=0, embedded_delta=1)
    paper = Paper(arxiv_id="2101.00001", title="Title", abstract="Abstract", authors=[], categories=[])

    upsert.upsert_papers([paper])

    apply_call = mock_run_write.call_args_list[1]
    assert apply_call.kwargs["new_papers"] == 1
    assert apply_call.kwargs["embedded_delta"] == 1


def test_upsert_paper_stubs_dedupes(mocker):
    mock_run_write = _mock_run_write(mocker)
    stubs = [
        CitationStub(arxiv_id="2001.00001", title="A"),
        CitationStub(arxiv_id="2001.00001", title="A duplicate"),
        CitationStub(s2_paper_id="s2-9", title="B"),
    ]

    upsert.upsert_paper_stubs(stubs)

    upsert_call = mock_run_write.call_args_list[0]
    ids = {s["id"] for s in upsert_call.kwargs["stubs"]}
    assert ids == {"2001.00001", "s2:s2-9"}


def test_apply_enrichment_builds_edges_and_stubs(mocker):
    """
    Verifies that apply_enrichment() correctly processes citation data from
    Semantic Scholar and calls the database write function with the right params
    """
    mock_run_write = _mock_run_write(mocker, citation_count=3)
    result = EnrichmentResult(
        paper_id="2101.00001",
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


def test_upsert_paper_stubs_includes_pmid(mocker):
    mock_run_write = _mock_run_write(mocker)
    stubs = [CitationStub(pmid="12345678", title="A PubMed paper")]

    upsert.upsert_paper_stubs(stubs)

    stub_params = mock_run_write.call_args_list[0].kwargs["stubs"][0]
    assert stub_params["id"] == "pmid:12345678"
    assert stub_params["pmid"] == "12345678"


def test_set_paper_embeddings_writes_and_bumps_stats(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    now = datetime(2024, 1, 1, 12, 0, 0)

    upsert.set_paper_embeddings([("2101.00001", [0.1, 0.2])], now)

    embed_call, stats_call = mock_run_write.call_args_list
    embedding_param = embed_call.kwargs["embeddings"][0]
    assert embedding_param["id"] == "2101.00001"
    assert embedding_param["embedding"] == [0.1, 0.2]
    assert embedding_param["embedded_at"] == now.isoformat()
    assert stats_call.kwargs["newly_embedded_count"] == 1


def test_set_paper_embeddings_noop_on_empty(mocker):
    mock_run_write = mocker.patch.object(upsert, "run_write")
    upsert.set_paper_embeddings([], datetime.now())
    mock_run_write.assert_not_called()
