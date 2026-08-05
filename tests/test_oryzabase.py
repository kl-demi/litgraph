from spokebio.ingest.oryzabase import (
    build_symbol_map,
    ensure_oryzabase_file,
    extract_associated_with,
    iter_gene_rows,
)
from spokebio.models import AssociatedWith
from spokebio.upsert import upsert_associated_with

_COLUMNS = [
    "Trait Gene Id", "CGSNL Gene Symbol", "Gene symbol synonym(s)", "CGSNL Gene Name",
    "Gene name synonym(s)", "Protein Name", "Allele", "Chromosome No.", "Explanation",
    "Trait Class", "RAP ID", "MSU ID", "Gramene ID", "Arm", "Locate(cM)",
    "Gene Ontology", "Trait Ontology", "Plant Ontology",
]


def _row(**overrides) -> str:
    values = dict.fromkeys(_COLUMNS, "")
    values.update(overrides)
    return "\t".join(values[c] for c in _COLUMNS)


# Trailing whitespace on the RAP id and a transcript-suffixed, comma-separated MSU id are
# both real formatting in the live export.
_RAP_ROW = _row(
    **{
        "CGSNL Gene Symbol": "BPH9",
        "RAP ID": "Os12g0559400 ",
        "MSU ID": "LOC_Os12g37280.1, LOC_Os12g37290.1",
        "Trait Ontology": "TO:0000276 - drought tolerance",
    }
)
# Multiple TO terms in one cell, comma-separated, one name containing its own parentheses.
_MULTI_TRAIT_ROW = _row(
    **{
        "CGSNL Gene Symbol": "GHD7",
        "RAP ID": "Os07g0261200",
        "Trait Ontology": "TO:0000280 - seedling vigor, TO:0000303 - cold tolerance, "
        "TO:0000232 - cytoplasmic male sterility (sensu Oryza)",
    }
)
# Only an MSU id -- must still resolve, via the un-suffixed form.
_MSU_ONLY_ROW = _row(
    **{"CGSNL Gene Symbol": "SOMEGENE", "MSU ID": "LOC_Os04g35210.1", "Trait Ontology": "TO:0006001 - salt tolerance"}
)
# A bracketed classical mutant with no locus id and no gene_info match: unresolvable.
_UNRESOLVABLE_ROW = _row(
    **{"CGSNL Gene Symbol": "[CMS-54257]", "Trait Ontology": "TO:0000232 - cytoplasmic male sterility"}
)
# No TO annotation at all -- skipped before gene resolution is even attempted.
_NO_TRAIT_ROW = _row(**{"CGSNL Gene Symbol": "NOTRAIT", "RAP ID": "Os01g0100100"})

_FIXTURE = (
    "\n".join(
        ["\t".join(_COLUMNS), _RAP_ROW, _MULTI_TRAIT_ROW, _MSU_ONLY_ROW, _UNRESOLVABLE_ROW, _NO_TRAIT_ROW]
    )
    + "\n"
)

_CROSSWALK = {
    "OS12G0559400": "ncbigene:4352133",
    "OS07G0261200": "ncbigene:4342860",
    "LOC_OS04G35210": "ncbigene:4336000",
}


def _write(tmp_path, text=_FIXTURE, encoding="utf-8-sig"):
    path = tmp_path / "gene_list.tsv"
    path.write_text(text, encoding=encoding)
    return path


def test_iter_gene_rows_parses_header_and_rows(tmp_path):
    rows = list(iter_gene_rows(_write(tmp_path)))

    assert len(rows) == 5
    assert rows[0]["CGSNL Gene Symbol"] == "BPH9"
    assert rows[0]["Trait Ontology"] == "TO:0000276 - drought tolerance"


def test_iter_gene_rows_strips_utf8_bom(tmp_path):
    """The live export is served with a BOM and a *mislabeled* Windows-31J charset. If
    the BOM leaks into the first header name, every lookup on that column silently
    returns nothing."""
    rows = list(iter_gene_rows(_write(tmp_path)))

    assert "Trait Gene Id" in rows[0]
    assert not any(k.startswith("﻿") for k in rows[0])


def test_extract_associated_with_resolves_rap_id_ignoring_trailing_whitespace(tmp_path):
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK)

    assert AssociatedWith(gene_id="ncbigene:4352133", trait_id="TO:0000276", source_db="Oryzabase") in extraction.edges


def test_extract_associated_with_expands_multiple_traits_per_row(tmp_path):
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK)

    traits = {e.trait_id for e in extraction.edges if e.gene_id == "ncbigene:4342860"}
    assert traits == {"TO:0000280", "TO:0000303", "TO:0000232"}


def test_extract_associated_with_resolves_msu_id_dropping_transcript_suffix(tmp_path):
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK)

    assert AssociatedWith(gene_id="ncbigene:4336000", trait_id="TO:0006001", source_db="Oryzabase") in extraction.edges


def test_extract_associated_with_counts_unresolvable_rows(tmp_path):
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK)

    assert extraction.rows_with_traits == 4  # the no-TO row never counts
    assert extraction.dropped_unresolved == 1
    assert all("CMS" not in e.gene_id for e in extraction.edges)


def test_extract_associated_with_skips_rows_without_a_trait(tmp_path):
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK)

    assert "ncbigene:4326000" not in {e.gene_id for e in extraction.edges}
    assert extraction.rows_with_traits == 4


def test_extract_associated_with_dedupes_repeated_gene_trait_pairs(tmp_path):
    duplicated = _FIXTURE + _RAP_ROW + "\n"
    extraction = extract_associated_with(_write(tmp_path, duplicated), _CROSSWALK)

    pairs = [(e.gene_id, e.trait_id) for e in extraction.edges]
    assert len(pairs) == len(set(pairs))
    assert extraction.dropped_duplicate == 1


def test_extract_associated_with_prefers_locus_id_over_ambiguous_symbol(tmp_path):
    """A symbol like SALT collides with an ordinary word and with other genes; the RAP id
    on the same row identifies exactly one locus. Locus ids must win."""
    row = _row(**{"CGSNL Gene Symbol": "SALT", "RAP ID": "Os12g0559400", "Trait Ontology": "TO:0006001 - salt"})
    fixture = "\n".join(["\t".join(_COLUMNS), row]) + "\n"
    crosswalk = {**_CROSSWALK, "SALT": "ncbigene:9999999"}

    extraction = extract_associated_with(_write(tmp_path, fixture), crosswalk)

    assert extraction.edges == [
        AssociatedWith(gene_id="ncbigene:4352133", trait_id="TO:0006001", source_db="Oryzabase")
    ]


def test_ensure_oryzabase_file_skips_download_if_already_cached(tmp_path, mocker):
    path = _write(tmp_path)
    mock_stream = mocker.patch("spokebio.ingest.oryzabase.httpx.stream")

    assert ensure_oryzabase_file(path) == str(path)
    mock_stream.assert_not_called()


def test_upsert_associated_with_writes_params(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    mock_run_write.return_value = [{"new_edges": 1}]

    new_count = upsert_associated_with(
        [AssociatedWith(gene_id="ncbigene:4352133", trait_id="TO:0000276", source_db="Oryzabase")]
    )

    assert new_count == 1
    assert mock_run_write.call_args.kwargs["edges"][0] == {
        "gene_id": "ncbigene:4352133",
        "trait_id": "TO:0000276",
        "source_db": "Oryzabase",
    }


def test_upsert_associated_with_noop_on_empty(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert upsert_associated_with([]) == 0
    mock_run_write.assert_not_called()


def test_extract_associated_with_counts_annotations_to_unknown_traits(tmp_path):
    """Oryzabase annotates against TO ids that TO has since obsoleted. upsert MATCHes the
    Trait, so those edges vanish regardless -- this makes the loss a reported number
    rather than a silent no-op."""
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK, known_trait_ids={"TO:0000276"})

    assert [e.trait_id for e in extraction.edges] == ["TO:0000276"]
    # GHD7's 3 terms + the MSU row's 1, all outside the known set
    assert extraction.dropped_unknown_trait == 4


def test_extract_associated_with_keeps_every_trait_when_check_is_omitted(tmp_path):
    extraction = extract_associated_with(_write(tmp_path), _CROSSWALK)

    assert extraction.dropped_unknown_trait == 0
    assert len(extraction.edges) == 5  # 1 + 3 + 1 across the three resolvable rows


def test_build_symbol_map_resolves_curated_symbols(tmp_path):
    """The naming source rice actually has: CGSNL symbols never reach NCBI's gene_info, so
    without this the graph shows a bare locus id for genes like GHD7."""
    symbols = build_symbol_map(_write(tmp_path), _CROSSWALK)

    assert symbols["ncbigene:4352133"] == "BPH9"
    assert symbols["ncbigene:4342860"] == "GHD7"
    assert symbols["ncbigene:4336000"] == "SOMEGENE"  # resolved via the MSU-only column


def test_build_symbol_map_covers_rows_with_no_trait_annotation(tmp_path):
    """Unlike extract_associated_with, naming must not require a TO term -- a gene with a
    curated symbol and no trait still deserves its name."""
    assert build_symbol_map(_write(tmp_path), {"OS01G0100100": "ncbigene:4400123"}) == {
        "ncbigene:4400123": "NOTRAIT"
    }


def test_build_symbol_map_strips_decorations_and_skips_unresolvable(tmp_path):
    symbols = build_symbol_map(_write(tmp_path), _CROSSWALK)

    # "[CMS-54257]" has no locus id and no crosswalk entry.
    assert not any(s.startswith("[") for s in symbols.values())
    assert all(v.startswith("ncbigene:") for v in symbols)


def test_build_symbol_map_skips_rows_with_no_symbol(tmp_path):
    """Oryzabase uses "_" for a row it has no CGSNL symbol for."""
    fixture = "\n".join(["\t".join(_COLUMNS), _row(**{"CGSNL Gene Symbol": "_", "RAP ID": "Os12g0559400"})]) + "\n"

    assert build_symbol_map(_write(tmp_path, fixture), _CROSSWALK) == {}
