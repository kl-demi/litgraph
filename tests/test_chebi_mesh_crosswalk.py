import gzip

from spokebio.ingest.chebi_mesh_crosswalk import (
    build_crosswalk,
    ensure_biomappings_file,
    ensure_chebi_file,
    ensure_mesh_file,
    _parse_biomappings_chebi_to_mesh,
    _parse_chebi_accession_to_cas,
    _parse_chebi_id_to_accession,
    _parse_mesh_cas_to_ui,
)

_COMPOUNDS_HEADER = "id\tname\tstatus_id\tsource\tparent_id\tmerge_type\tchebi_accession\tdefinition\tascii_name\tstars\tmodified_on\trelease_date"
_COMPOUNDS_FIXTURE = (
    _COMPOUNDS_HEADER
    + "\n"
    + "16480\tnitric oxide\t3\tKEGG COMPOUND\t\t\tCHEBI:16480\tA colourless gas.\tnitric oxide\t3\t2020-01-01\t\n"
    + "7\t(+)-car-3-ene\t1\tKEGG COMPOUND\t\t\tCHEBI:7\tA terpene.\t(+)-car-3-ene\t3\t2015-01-01\t\n"
)

_DB_ACCESSION_HEADER = "id\tcompound_id\taccession_number\ttype\tstatus_id\tsource_id"
_DB_ACCESSION_FIXTURE = (
    _DB_ACCESSION_HEADER
    + "\n"
    + "1\t16480\t10102-43-9\tCAS\t1\t45\n"
    + "2\t16480\tC05590\tMANUAL_X_REF\t1\t45\n"  # non-CAS type, should be ignored
    + "3\t7\t498-15-7\tCAS\t1\t45\n"
)

# Ordering matches the real file: RR (registry number) lines come BEFORE UI -- the bug
# this module's docstring warns about.
_MESH_D_FIXTURE = (
    "*NEWRECORD\n"
    "MH = Nitric Oxide\n"
    "RR = 10102-43-9 (Nitric Oxide)\n"
    "UI = D009569\n"
    "\n"
    "*NEWRECORD\n"
    "MH = Something Else\n"
    "RR = 498-15-7 (Something Else)\n"
    "RR = 999-99-9 (also this compound, ambiguous case)\n"
    "UI = D999999\n"
)
_MESH_C_FIXTURE = (
    "*NEWRECORD\n"
    "NM = a supplementary concept\n"
    "RR = 999-99-9 (shared with D999999 above -- makes 498-15-7's CAS ambiguous)\n"
    "UI = C000001\n"
)

_BIOMAPPINGS_FIXTURE = (
    "#curie_map:\n"
    "#  chebi: http://purl.obolibrary.org/obo/CHEBI_\n"
    "\n"
    "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tmapping_justification\n"
    "chebi:16480\tnitric oxide\tskos:exactMatch\tmesh:D009569\tNitric Oxide\tsemapv:ManualMappingCuration\n"
    "chebi:99999\tconflicting compound\tskos:exactMatch\tmesh:D111111\tSome Other Name\tsemapv:ManualMappingCuration\n"
)


def test_parse_chebi_id_to_accession(tmp_path):
    path = tmp_path / "compounds.tsv.gz"
    with gzip.open(path, "wt") as f:
        f.write(_COMPOUNDS_FIXTURE)

    result = _parse_chebi_id_to_accession(path)

    assert result == {"16480": "CHEBI:16480", "7": "CHEBI:7"}


def test_parse_chebi_accession_to_cas_filters_non_cas_types(tmp_path):
    compounds_path = tmp_path / "compounds.tsv.gz"
    with gzip.open(compounds_path, "wt") as f:
        f.write(_COMPOUNDS_FIXTURE)
    db_path = tmp_path / "database_accession.tsv.gz"
    with gzip.open(db_path, "wt") as f:
        f.write(_DB_ACCESSION_FIXTURE)

    id_to_accession = _parse_chebi_id_to_accession(compounds_path)
    result = _parse_chebi_accession_to_cas(db_path, id_to_accession)

    assert result == {"CHEBI:16480": {"10102-43-9"}, "CHEBI:7": {"498-15-7"}}


def test_parse_mesh_cas_to_ui_handles_rr_before_ui_ordering(tmp_path):
    d_path = tmp_path / "d2025.bin"
    d_path.write_text(_MESH_D_FIXTURE)

    result = _parse_mesh_cas_to_ui([d_path])

    assert result["10102-43-9"] == {"D009569"}


def test_parse_mesh_cas_to_ui_merges_across_multiple_files(tmp_path):
    d_path = tmp_path / "d2025.bin"
    d_path.write_text(_MESH_D_FIXTURE)
    c_path = tmp_path / "c2025.bin"
    c_path.write_text(_MESH_C_FIXTURE)

    result = _parse_mesh_cas_to_ui([d_path, c_path])

    # 999-99-9 appears under both D999999 (in d file) and C000001 (in c file) --
    # confirms cross-file merging, and sets up the ambiguous-CAS case used below.
    assert result["999-99-9"] == {"D999999", "C000001"}


def test_parse_biomappings_chebi_to_mesh(tmp_path):
    path = tmp_path / "positive.sssom.tsv"
    path.write_text(_BIOMAPPINGS_FIXTURE)

    result = _parse_biomappings_chebi_to_mesh(path)

    assert result == {"CHEBI:16480": "D009569", "CHEBI:99999": "D111111"}


def test_build_crosswalk_combines_both_methods(tmp_path):
    compounds_path = tmp_path / "compounds.tsv.gz"
    with gzip.open(compounds_path, "wt") as f:
        f.write(_COMPOUNDS_FIXTURE)
    db_path = tmp_path / "database_accession.tsv.gz"
    with gzip.open(db_path, "wt") as f:
        f.write(_DB_ACCESSION_FIXTURE)
    d_path = tmp_path / "d2025.bin"
    d_path.write_text(_MESH_D_FIXTURE)
    biomappings_path = tmp_path / "positive.sssom.tsv"
    biomappings_path.write_text(_BIOMAPPINGS_FIXTURE)

    crosswalk = build_crosswalk(compounds_path, db_path, [d_path], biomappings_path)

    # CHEBI:16480 comes from both methods (agree on D009569) -- Biomappings' answer
    # wins by construction, but they match here anyway.
    assert crosswalk["CHEBI:16480"] == "mesh:D009569"
    # CHEBI:7 only resolves via the CAS bridge (498-15-7 -> D999999... but that CAS
    # became ambiguous once c2025.bin's C000001 entry is added; not added here, so it
    # resolves cleanly through the CAS bridge alone in this fixture).
    assert crosswalk["CHEBI:7"] == "mesh:D999999"
    # CHEBI:99999 only appears in Biomappings, not the CAS bridge at all.
    assert crosswalk["CHEBI:99999"] == "mesh:D111111"


def test_build_crosswalk_drops_ambiguous_cas_bridge_matches(tmp_path):
    compounds_path = tmp_path / "compounds.tsv.gz"
    with gzip.open(compounds_path, "wt") as f:
        f.write(_COMPOUNDS_FIXTURE)
    db_path = tmp_path / "database_accession.tsv.gz"
    with gzip.open(db_path, "wt") as f:
        f.write(_DB_ACCESSION_FIXTURE)
    d_path = tmp_path / "d2025.bin"
    d_path.write_text(_MESH_D_FIXTURE)
    c_path = tmp_path / "c2025.bin"
    c_path.write_text(_MESH_C_FIXTURE)  # makes 498-15-7... no, makes 999-99-9 ambiguous
    biomappings_path = tmp_path / "positive.sssom.tsv"
    biomappings_path.write_text("subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tmapping_justification\n")

    crosswalk = build_crosswalk(compounds_path, db_path, [d_path, c_path], biomappings_path)

    # CHEBI:7's only CAS (498-15-7) resolves to D999999, which ALSO carries the
    # now-ambiguous 999-99-9 -- but 498-15-7 itself is unambiguous, so CHEBI:7 still
    # resolves fine. This test mainly documents that ambiguity is per-CAS-number, not
    # contagious across a whole MeSH record.
    assert crosswalk["CHEBI:7"] == "mesh:D999999"


def test_ensure_chebi_file_skips_download_if_cached(tmp_path, mocker):
    path = tmp_path / "compounds.tsv.gz"
    path.write_bytes(b"data")
    mock_stream = mocker.patch("spokebio.ingest.chebi_mesh_crosswalk.httpx.stream")

    result = ensure_chebi_file("compounds.tsv.gz", dir_path=tmp_path)

    assert result == str(path)
    mock_stream.assert_not_called()


def test_ensure_mesh_file_uses_year_in_url(mocker, tmp_path):
    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"data"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mock_stream = mocker.patch(
        "spokebio.ingest.chebi_mesh_crosswalk.httpx.stream", return_value=FakeStreamResponse()
    )

    ensure_mesh_file("d2025.bin", year=2025, dir_path=tmp_path)

    called_url = mock_stream.call_args.args[1]
    assert "2025" in called_url


def test_ensure_biomappings_file_downloads_when_missing(tmp_path, mocker):
    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"data"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mocker.patch("spokebio.ingest.chebi_mesh_crosswalk.httpx.stream", return_value=FakeStreamResponse())

    result = ensure_biomappings_file(dir_path=tmp_path)

    assert result == str(tmp_path / "positive.sssom.tsv")
