from datetime import datetime

import pytest

from litgraph.graph import upsert
from litgraph.graph.writer import CreateMissing
from litgraph.models import PAPER_IDENTIFIERS, CitationStub, EnrichmentResult, Paper, arxiv_category, mesh_heading

_STATS_DELTA_KEYS = (
    "new_papers",
    "upgraded_stubs",
    "embedded_delta",
    "batch_min_date",
    "batch_max_date",
    "new_categories",
    "new_edges",
    "new_authors",
    "new_stubs",
    "newly_enriched_count",
)


def _mock_run_write(mocker, **overrides):
    """Mock run_write so every call returns one delta row a stats-apply call can consume as
    `**kwargs`."""
    row = {key: None if key.endswith("_date") else 0 for key in _STATS_DELTA_KEYS} | overrides
    mock = mocker.patch.object(upsert, "run_write")
    mock.return_value = [row]
    return mock


def _kwargs_containing(mock, key):
    return next(call.kwargs for call in mock.call_args_list if key in call.kwargs)


# --- Paper params -----------------------------------------------------------------------


def test_paper_params_carry_the_namespaced_id():
    params = upsert._paper_params(Paper(arxiv_id="2101.00001", title="T"))
    assert params["id"] == "arxiv:2101.00001"


@pytest.mark.parametrize("namespace", PAPER_IDENTIFIERS, ids=lambda ns: ns.prefix)
def test_paper_params_include_every_identifier_column(namespace):
    """Every column is written on every upsert, so an absent namespace must appear as None
    rather than be omitted."""
    params = upsert._paper_params(Paper(pmid="12345678", title="T"))
    assert namespace.column in params


def test_paper_params_null_absent_identifier_columns():
    params = upsert._paper_params(Paper(pmid="12345678", title="T"))
    assert (params["arxiv_id"], params["pmid"], params["s2_paper_id"]) == (None, "12345678", None)


def test_paper_params_store_categories_as_a_flat_code_array():
    """Keeps `$category IN p.categories` working; vocabulary and name live on the Category
    node instead."""
    paper = Paper(arxiv_id="2101.00001", title="T", categories=[arxiv_category("cs.CL"), mesh_heading("D1", "One")])
    assert upsert._paper_params(paper)["categories"] == ["arxiv:cs.CL", "mesh:D1"]


def test_paper_params_send_source_as_a_plain_string():
    """The Bolt driver would otherwise send an enum object where the vertex expects a
    string."""
    source = upsert._paper_params(Paper(arxiv_id="2101.00001", title="T", source="pubmed"))["source"]
    assert source == "pubmed" and type(source) is str


def test_paper_params_isoformat_dates_and_timestamps():
    paper = Paper(arxiv_id="2101.00001", title="T", fetched_at=datetime(2024, 1, 2, 3, 4, 5))
    params = upsert._paper_params(paper)
    assert params["fetched_at"] == "2024-01-02T03:04:05"
    assert params["published_date"] is None


# --- Category params --------------------------------------------------------------------


def test_category_params_flatten_one_row_per_pair():
    paper = Paper(arxiv_id="2101.00001", title="T", categories=[mesh_heading("D009422", "Nervous System")])
    assert upsert._category_params([paper]) == [
        {
            "paper_id": "arxiv:2101.00001",
            "code": "mesh:D009422",
            "vocabulary": "mesh",
            "name": "Nervous System",
        }
    ]


def test_category_params_dedupe_a_repeated_code():
    """A duplicate would double-count Category.paper_count on first write."""
    paper = Paper(arxiv_id="2101.00001", title="T", categories=[arxiv_category("cs.CL"), arxiv_category("cs.CL")])
    assert len(upsert._category_params([paper])) == 1


def test_category_params_keep_the_same_code_on_different_papers():
    papers = [Paper(arxiv_id=f"2101.0000{i}", title="T", categories=[arxiv_category("cs.CL")]) for i in (1, 2)]
    assert len(upsert._category_params(papers)) == 2


def test_category_params_are_empty_without_categories():
    assert upsert._category_params([Paper(arxiv_id="2101.00001", title="T")]) == []


# --- upsert_papers ----------------------------------------------------------------------


def test_upsert_papers_writes_papers_categories_and_authors(mocker):
    mock = _mock_run_write(mocker)
    paper = Paper(
        arxiv_id="2101.00001",
        title="Title",
        authors=["Jane Doe"],
        categories=[arxiv_category("cs.CL")],
    )

    upsert.upsert_papers([paper])

    queries = [call.args[0] for call in mock.call_args_list]
    assert len(queries) == 6  # three upserts, each followed by its GraphStats apply
    assert "MERGE (paper:Paper {id: p.id})" in queries[0]
    assert "MERGE (c:Category {code: cat.code})" in queries[2]
    assert "MERGE (a:Author {name: authorName})" in queries[4]
    assert all("GraphStats" in queries[i] for i in (1, 3, 5))


def test_upsert_papers_passes_categories_as_their_own_param(mocker):
    """Flattened to a top-level list rather than nested inside `$papers`, which ArcadeDB's
    Cypher layer handles unreliably."""
    mock = _mock_run_write(mocker)
    paper = Paper(arxiv_id="2101.00001", title="T", categories=[arxiv_category("cs.CL")])

    upsert.upsert_papers([paper])

    assert _kwargs_containing(mock, "categories")["categories"][0]["code"] == "arxiv:cs.CL"


def test_upsert_papers_skips_the_category_write_when_uncategorized(mocker):
    mock = _mock_run_write(mocker)
    upsert.upsert_papers([Paper(arxiv_id="2101.00001", title="T")])
    assert not any("categories" in call.kwargs for call in mock.call_args_list)


def test_upsert_papers_threads_the_stats_delta_into_the_apply_call(mocker):
    mock = _mock_run_write(mocker, new_papers=1, embedded_delta=1)
    upsert.upsert_papers([Paper(arxiv_id="2101.00001", title="T")])

    apply_call = mock.call_args_list[1]
    assert (apply_call.kwargs["new_papers"], apply_call.kwargs["embedded_delta"]) == (1, 1)


def test_upsert_papers_noop_on_empty(mocker):
    mock = mocker.patch.object(upsert, "run_write")
    upsert.upsert_papers([])
    mock.assert_not_called()


# --- Stubs and citation edges -----------------------------------------------------------


def test_upsert_paper_stubs_dedupes_by_graph_id(mocker):
    mock_write = _mock_run_write(mocker)
    nodes = mocker.patch.object(upsert, "upsert_nodes", return_value=0)
    stubs = [
        CitationStub(arxiv_id="2001.00001", title="A"),
        CitationStub(arxiv_id="2001.00001", title="A duplicate"),
        CitationStub(s2_paper_id="s2-9", title="B"),
    ]

    upsert.upsert_paper_stubs(stubs)

    assert {row["id"] for row in nodes.call_args.args[1]} == {"arxiv:2001.00001", "s2:s2-9"}
    assert mock_write.call_args.kwargs["new_stubs"] == 0


def test_upsert_paper_stubs_never_update_an_existing_paper(mocker):
    """A stub target that is already fully ingested would otherwise have its fields blanked
    and is_stub flipped back to true."""
    _mock_run_write(mocker)
    nodes = mocker.patch.object(upsert, "upsert_nodes", return_value=0)

    upsert.upsert_paper_stubs([CitationStub(arxiv_id="2001.00001", title="A")])

    assert nodes.call_args.kwargs["update_existing"] is False
    assert nodes.call_args.args[0] == "Paper"


def test_upsert_paper_stubs_carry_every_identifier_column(mocker):
    _mock_run_write(mocker)
    nodes = mocker.patch.object(upsert, "upsert_nodes", return_value=0)

    upsert.upsert_paper_stubs([CitationStub(pmid="12345678", title="A PubMed paper")])

    row = nodes.call_args.args[1][0]
    assert row["id"] == "pmid:12345678"
    assert row["pmid"] == "12345678"
    assert row["arxiv_id"] is None
    assert row["is_stub"] is True


def test_upsert_paper_stubs_noop_on_empty(mocker):
    mock = mocker.patch.object(upsert, "run_write")
    upsert.upsert_paper_stubs([])
    mock.assert_not_called()


def test_upsert_citation_edges_dedupes(mocker):
    _mock_run_write(mocker)
    edges = mocker.patch.object(upsert, "upsert_edges", return_value=0)

    upsert.upsert_citation_edges([("arxiv:a", "arxiv:b"), ("arxiv:a", "arxiv:b")])

    assert edges.call_args.args[1] == [{"src": "arxiv:a", "dst": "arxiv:b"}]


def test_upsert_citation_edges_require_both_papers_to_exist(mocker):
    """upsert_paper_stubs runs first and is what creates a missing endpoint, with its title
    and identifiers attached."""
    _mock_run_write(mocker)
    edges = mocker.patch.object(upsert, "upsert_edges", return_value=0)

    upsert.upsert_citation_edges([("arxiv:a", "arxiv:b")])

    assert edges.call_args.kwargs["create_missing"] is CreateMissing.NONE
    assert edges.call_args.args[0] == "CITES"


def test_upsert_citation_edges_noop_on_empty(mocker):
    mock = mocker.patch.object(upsert, "run_write")
    upsert.upsert_citation_edges([])
    mock.assert_not_called()


# --- Enrichment -------------------------------------------------------------------------


def test_apply_enrichment_builds_stubs_and_edges_in_both_directions(mocker):
    mock_write = _mock_run_write(mocker)
    nodes = mocker.patch.object(upsert, "upsert_nodes", return_value=0)
    edges = mocker.patch.object(upsert, "upsert_edges", return_value=0)
    result = EnrichmentResult(
        paper_id="arxiv:2101.00001",
        s2_paper_id="s2-1",
        citation_count=3,
        references=[CitationStub(arxiv_id="2001.00001", title="Ref")],
        citations=[CitationStub(s2_paper_id="s2-2", title="Citer")],
    )

    upsert.apply_enrichment([result])

    assert {row["id"] for row in nodes.call_args.args[1]} == {"arxiv:2001.00001", "s2:s2-2"}
    assert {(row["src"], row["dst"]) for row in edges.call_args.args[1]} == {
        ("arxiv:2101.00001", "arxiv:2001.00001"),
        ("s2:s2-2", "arxiv:2101.00001"),
    }
    assert _kwargs_containing(mock_write, "results")["results"][0]["citation_count"] == 3


def test_apply_enrichment_noop_on_empty(mocker):
    mock = mocker.patch.object(upsert, "run_write")
    upsert.apply_enrichment([])
    mock.assert_not_called()


# --- Embeddings -------------------------------------------------------------------------


def test_set_paper_embeddings_writes_vectors_and_bumps_stats(mocker):
    mock = mocker.patch.object(upsert, "run_write")
    now = datetime(2024, 1, 1, 12, 0, 0)

    upsert.set_paper_embeddings([("arxiv:2101.00001", [0.1, 0.2])], now)

    embed_call, stats_call = mock.call_args_list
    assert embed_call.kwargs["embeddings"] == [
        {"id": "arxiv:2101.00001", "embedding": [0.1, 0.2], "embedded_at": now.isoformat()}
    ]
    assert stats_call.kwargs["newly_embedded_count"] == 1


def test_set_paper_embeddings_touches_only_embedding_fields():
    """Reusing upsert_papers here would blank every other field it doesn't reconstruct."""
    assert "paper.title" not in upsert._SET_EMBEDDINGS
    assert "paper.embedding" in upsert._SET_EMBEDDINGS


def test_set_paper_embeddings_noop_on_empty(mocker):
    mock = mocker.patch.object(upsert, "run_write")
    upsert.set_paper_embeddings([], datetime.now())
    mock.assert_not_called()


# --- Generated query text ---------------------------------------------------------------


@pytest.mark.parametrize("namespace", PAPER_IDENTIFIERS, ids=lambda ns: ns.prefix)
def test_identifier_columns_appear_in_the_paper_upsert(namespace):
    """Generated from PAPER_IDENTIFIERS, so a new paper source needs no query edit."""
    assert f"paper.{namespace.column} = p.{namespace.column}" in upsert._UPSERT_PAPERS


def test_category_upsert_writes_vocabulary_and_name():
    assert "SET c.vocabulary = cat.vocabulary, c.name = cat.name" in upsert._UPSERT_CATEGORIES
