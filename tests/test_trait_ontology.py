from spokebio.ingest.go import iter_term_stanzas
from spokebio.ingest.trait_ontology import ensure_to_obo_file, extract_traits
from spokebio.models import Trait
from spokebio.upsert import upsert_traits

# Mirrors the real to.obo's shape: TO's own terms interleaved with imported terms from
# other ontologies, and no per-term `namespace` line (to.obo declares only a single
# default-namespace in its header).
_TO_OBO_FIXTURE = """format-version: 1.2
data-version: releases/2026-01-14
default-namespace: plant_trait_ontology

[Term]
id: TO:0000276
name: drought tolerance
def: "Becoming tolerant to drought like conditions." [Gramene:pankaj_jaiswal]
synonym: "drought resistance (exact)" EXACT []
is_a: TO:0000394 ! drought stress response trait

[Term]
id: PO:0009066
name: anther

[Term]
id: CHEBI:24431
name: chemical entity

[Term]
id: GO:0009611
name: response to wounding

[Term]
id: TO:0000164
name: obsolete grain colour
is_obsolete: true

[Typedef]
id: part_of
name: part of

[Term]
id: TO:0006001
name: salt tolerance
"""


def test_extract_traits_keeps_only_non_obsolete_to_terms(tmp_path):
    obo_file = tmp_path / "to.obo"
    obo_file.write_text(_TO_OBO_FIXTURE)

    traits = list(extract_traits(iter_term_stanzas(obo_file)))

    assert traits == [
        Trait(trait_id="TO:0000276", name="drought tolerance", source_db="TO"),
        Trait(trait_id="TO:0006001", name="salt tolerance", source_db="TO"),
    ]


def test_extract_traits_drops_imported_terms_from_other_ontologies(tmp_path):
    """The PO/CHEBI/GO terms in to.obo are imports, not traits. Loading them would key
    Trait nodes on ids that Pathway (GO) and Compound (CHEBI) already model -- the
    duplicate-namespace failure docs/spoke_schema.md's Design Principle 5 warns about.
    """
    obo_file = tmp_path / "to.obo"
    obo_file.write_text(_TO_OBO_FIXTURE)

    trait_ids = {t.trait_id for t in extract_traits(iter_term_stanzas(obo_file))}

    assert trait_ids == {"TO:0000276", "TO:0006001"}
    assert not any(t.startswith(("PO:", "CHEBI:", "GO:")) for t in trait_ids)


def test_extract_traits_tolerates_missing_namespace_line(tmp_path):
    """go.py's extract_pathways filters on `namespace`; to.obo has no per-term namespace
    line, so every stanza parses as namespace=None. Filtering on it here would yield
    nothing at all."""
    obo_file = tmp_path / "to.obo"
    obo_file.write_text(_TO_OBO_FIXTURE)

    stanzas = list(iter_term_stanzas(obo_file))

    assert all(s["namespace"] is None for s in stanzas)
    assert len(list(extract_traits(iter(stanzas)))) == 2


def test_ensure_to_obo_file_skips_download_if_already_cached(tmp_path, mocker):
    obo_file = tmp_path / "to.obo"
    obo_file.write_text(_TO_OBO_FIXTURE)
    mock_stream = mocker.patch("spokebio.ingest.trait_ontology.httpx.stream")

    assert ensure_to_obo_file(obo_file) == str(obo_file)
    mock_stream.assert_not_called()


def test_ensure_to_obo_file_downloads_when_missing(tmp_path, mocker):
    obo_file = tmp_path / "subdir" / "to.obo"

    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield _TO_OBO_FIXTURE.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mocker.patch("spokebio.ingest.trait_ontology.httpx.stream", return_value=FakeStreamResponse())

    assert ensure_to_obo_file(obo_file) == str(obo_file)
    assert obo_file.read_text() == _TO_OBO_FIXTURE


def test_upsert_traits_writes_params(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    mock_run_write.return_value = [{"new_traits": 2}]

    new_count = upsert_traits(
        [
            Trait(trait_id="TO:0000276", name="drought tolerance", source_db="TO"),
            Trait(trait_id="TO:0006001", name="salt tolerance", source_db="TO"),
        ]
    )

    assert new_count == 2
    assert mock_run_write.call_args.kwargs["traits"][0] == {
        "trait_id": "TO:0000276",
        "name": "drought tolerance",
        "source_db": "TO",
    }


def test_upsert_traits_noop_on_empty(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert upsert_traits([]) == 0
    mock_run_write.assert_not_called()
