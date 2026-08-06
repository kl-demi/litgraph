import pytest
from pydantic import ValidationError

from litgraph.models import (
    PAPER_IDENTIFIERS,
    Category,
    CategoryVocabulary,
    CitationStub,
    Paper,
    Source,
    arxiv_category,
    identifier_columns,
    mesh_heading,
)

# --- Paper identity ---------------------------------------------------------------------


def test_id_prefers_arxiv_over_later_namespaces():
    paper = Paper(arxiv_id="2101.00001", pmid="12345678", s2_paper_id="s2-1", title="T")
    assert paper.id == "arxiv:2101.00001"


def test_id_falls_through_the_preference_order():
    assert Paper(pmid="12345678", s2_paper_id="s2-1", title="T").id == "pmid:12345678"
    assert Paper(s2_paper_id="s2-1", title="T").id == "s2:s2-1"


@pytest.mark.parametrize("namespace", PAPER_IDENTIFIERS, ids=lambda ns: ns.prefix)
def test_every_namespace_is_prefixed(namespace):
    """`prefix` is both the `identifiers` key and the id prefix, so an unprefixed namespace
    would be one with no key."""
    paper = Paper(identifiers={namespace.prefix: "X"}, title="T")
    assert paper.id == f"{namespace.prefix}:X"


def test_flat_kwargs_fold_into_identifiers():
    paper = Paper(arxiv_id="2101.00001", pmid="12345678", title="T")
    assert paper.identifiers == {"arxiv": "2101.00001", "pmid": "12345678"}


def test_flat_kwargs_round_trip_through_properties():
    paper = Paper(pmid="12345678", title="T")
    assert (paper.arxiv_id, paper.pmid, paper.s2_paper_id) == (None, "12345678", None)


def test_identifiers_can_be_passed_directly():
    assert Paper(identifiers={"pmid": "12345678"}, title="T").id == "pmid:12345678"


def test_a_flat_kwarg_set_to_none_is_not_stored():
    """Storing it would make `Paper.id` return the string "pmid:None"."""
    assert Paper(arxiv_id="2101.00001", pmid=None, title="T").identifiers == {"arxiv": "2101.00001"}


def test_non_string_identifier_is_coerced():
    assert Paper(pmid=12345678, title="T").identifiers == {"pmid": "12345678"}


def test_paper_requires_an_identifier():
    with pytest.raises(ValidationError):
        Paper(title="T")


def test_unregistered_namespace_is_rejected():
    with pytest.raises(ValidationError, match="unregistered identifier namespace"):
        Paper(identifiers={"openalex": "W123"}, title="T")


def test_identifier_columns_yields_absent_namespaces_as_none():
    """`_UPSERT_PAPERS` SETs every column unconditionally, so a missing key would leave a
    stale value from an earlier write in place."""
    assert dict(identifier_columns({"pmid": "12345678"})) == {
        "arxiv_id": None,
        "pmid": "12345678",
        "s2_paper_id": None,
    }


def test_column_differs_from_prefix():
    """The vertex property is `arxiv_id` while the namespace is `arxiv`, which is why
    `column` stays a separate field."""
    arxiv = next(ns for ns in PAPER_IDENTIFIERS if ns.prefix == "arxiv")
    assert (arxiv.column, arxiv.prefix) == ("arxiv_id", "arxiv")


def test_column_doubles_as_the_accepted_constructor_kwarg():
    """Pydantic ignores unknown kwargs silently, so a column renamed without its call sites
    would drop ids rather than raise."""
    for namespace in PAPER_IDENTIFIERS:
        paper = Paper(title="T", **{namespace.column: "X"})
        assert paper.identifiers == {namespace.prefix: "X"}


# --- CitationStub -----------------------------------------------------------------------


def test_stub_shares_paper_identifier_handling():
    assert CitationStub(arxiv_id="2001.00001").id == "arxiv:2001.00001"
    assert CitationStub(pmid="12345678").id == "pmid:12345678"
    assert CitationStub(s2_paper_id="s2-2").id == "s2:s2-2"


def test_stub_without_an_identifier_raises_on_id():
    with pytest.raises(ValueError):
        CitationStub(title="T").id


# --- Source -----------------------------------------------------------------------------


def test_source_accepts_a_known_string_and_compares_equal_to_it():
    paper = Paper(arxiv_id="2101.00001", title="T", source="pubmed")
    assert paper.source is Source.PUBMED
    assert paper.source == "pubmed"


def test_source_value_is_the_plain_string_written_to_the_vertex():
    assert Source.PUBMED_BASELINE.value == "pubmed_baseline"


def test_unknown_source_is_rejected():
    with pytest.raises(ValidationError):
        Paper(arxiv_id="2101.00001", title="T", source="pubmed_basline")


def test_source_defaults_to_arxiv():
    assert Paper(arxiv_id="2101.00001", title="T").source is Source.ARXIV


# --- Category ---------------------------------------------------------------------------


def test_arxiv_category_is_namespaced_and_self_named():
    category = arxiv_category("cs.CL")
    assert (category.code, category.vocabulary, category.name) == ("arxiv:cs.CL", CategoryVocabulary.ARXIV, "cs.CL")


def test_mesh_heading_is_keyed_on_the_ui_not_the_name():
    heading = mesh_heading("D009422", "Nervous System")
    assert (heading.code, heading.vocabulary, heading.name) == ("mesh:D009422", CategoryVocabulary.MESH, "Nervous System")


def test_the_two_vocabularies_cannot_collide_on_one_code():
    """`Category.code` is a single global unique index across both vocabularies."""
    assert arxiv_category("Humans").code != mesh_heading("D006801", "Humans").code


def test_unknown_vocabulary_is_rejected():
    with pytest.raises(ValidationError):
        Category(vocabulary="scopus", code="scopus:1", name="One")


def test_category_codes_returns_the_flat_array_stored_on_the_vertex():
    paper = Paper(arxiv_id="2101.00001", title="T", categories=[arxiv_category("cs.CL"), mesh_heading("D1", "One")])
    assert paper.category_codes() == ["arxiv:cs.CL", "mesh:D1"]


def test_category_codes_is_empty_for_an_uncategorized_paper():
    assert Paper(arxiv_id="2101.00001", title="T").category_codes() == []
