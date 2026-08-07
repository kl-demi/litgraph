from spokebio.ingest.gene_gazetteer import (
    build_gazetteer,
    extract_mentions,
    find_gene_mentions,
    is_admissible,
    is_rice_specific,
)
from spokebio.models import EntityMention

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


_CROSSWALK = {
    "OS12G0559400": "ncbigene:4352133",
    "OS07G0261200": "ncbigene:4342860",
    "LOC_OS04G35210": "ncbigene:4336000",
    "SALT": "ncbigene:9999999",
}

_FIXTURE = (
    "\n".join(
        [
            "\t".join(_COLUMNS),
            _row(**{"CGSNL Gene Symbol": "BPH9", "Gene symbol synonym(s)": "Bph6, OsBPH6", "RAP ID": "Os12g0559400 "}),
            _row(**{"CGSNL Gene Symbol": "GHD7", "RAP ID": "Os07g0261200"}),
            _row(**{"CGSNL Gene Symbol": "SOMEGENE", "MSU ID": "LOC_Os04g35210.1"}),
            # An ambiguous all-alpha symbol that resolves: must NOT enter the gazetteer.
            _row(**{"CGSNL Gene Symbol": "SALT", "RAP ID": "Os12g0559400"}),
        ]
    )
    + "\n"
)


def _write(tmp_path, text=_FIXTURE):
    path = tmp_path / "gene_list.tsv"
    path.write_text(text, encoding="utf-8-sig")
    return path


# --- the ambiguity tier ------------------------------------------------------------


def test_is_unambiguous_accepts_rice_locus_ids():
    assert is_admissible("OS01G0194300")
    assert is_admissible("LOC_OS01G05060")


def test_is_unambiguous_accepts_os_prefixed_and_alphanumeric_symbols():
    assert is_admissible("OSNRAMP5")
    assert is_admissible("OSHKT1")
    assert is_admissible("GHD7")
    assert is_admissible("XA21")
    assert is_admissible("HD3A")


def test_is_unambiguous_rejects_ordinary_english_words():
    """These are the real top-frequency false positives from a 6,000-paper sample --
    SALT alone matched 979 times as the substance, not the gene."""
    for word in ("SALT", "POT", "ACT", "DWARF", "SPIKE", "LOG", "CAS", "OAT", "ARG"):
        assert not is_admissible(word), word


def test_is_unambiguous_rejects_short_alpha_symbols():
    assert not is_admissible("CO")
    assert not is_admissible("PAL")


# --- tokenization: the thing that silently cost 4 points of recall -----------------


def test_find_gene_mentions_matches_symbol_inside_a_hyphenated_compound():
    """"GHD7-mediated" must still yield GHD7. A tokenizer treating "-" as a word character
    misses this, which is exactly the bug that cost ~4pp of recall."""
    gaz = {"GHD7": "ncbigene:4342860"}
    assert find_gene_mentions("GHD7-mediated flowering", gaz) == {"ncbigene:4342860": "GHD7"}


def test_find_gene_mentions_matches_a_form_that_itself_contains_a_hyphen():
    """The converse: splitting on hyphens up front would destroy "Pi-ta"."""
    gaz = {"PI-TA": "ncbigene:111"}
    assert find_gene_mentions("the Pi-ta resistance gene", gaz) == {"ncbigene:111": "PI-TA"}


def test_find_gene_mentions_strips_transcript_suffix_and_trailing_punctuation():
    gaz = {"LOC_OS04G35210": "ncbigene:4336000"}
    assert find_gene_mentions("expressed from LOC_Os04g35210.1, as shown", gaz)
    assert find_gene_mentions("we studied LOC_Os04g35210.", gaz)


def test_find_gene_mentions_is_case_insensitive():
    gaz = {"OSNRAMP5": "ncbigene:222"}
    assert find_gene_mentions("OsNRAMP5 and osnramp5", gaz) == {"ncbigene:222": "OSNRAMP5"}


def test_find_gene_mentions_does_not_match_a_substring_of_a_longer_word():
    """A bare substring match would fire on unrelated tokens; only whole spans and their
    hyphen/dot pieces (plus embedded letters+digits symbols) count."""
    gaz = {"GHD7": "ncbigene:4342860"}
    assert find_gene_mentions("AGHD7X", gaz) == {}


def test_find_gene_mentions_reports_one_entry_per_gene_not_per_occurrence():
    gaz = {"GHD7": "ncbigene:4342860", "OSGHD7": "ncbigene:4342860"}
    found = find_gene_mentions("GHD7 and OsGhd7 and GHD7 again", gaz)
    assert found == {"ncbigene:4342860": "GHD7"}


def test_find_gene_mentions_tolerates_empty_text():
    assert find_gene_mentions("", {"GHD7": "x"}) == {}
    assert find_gene_mentions(None, {"GHD7": "x"}) == {}


# --- gazetteer construction --------------------------------------------------------


def test_build_gazetteer_admits_only_unambiguous_forms(tmp_path):
    gaz = build_gazetteer(_write(tmp_path), _CROSSWALK)

    assert "OS12G0559400" in gaz
    assert "GHD7" in gaz
    assert "OSBPH6" in gaz
    assert "SALT" not in gaz, "an ordinary English word must never enter the gazetteer"


def test_build_gazetteer_resolves_via_locus_id_over_symbol(tmp_path):
    """The SALT row carries RAP id Os12g0559400; its forms must resolve to that gene, not to
    the SALT symbol's own crosswalk entry."""
    gaz = build_gazetteer(_write(tmp_path), _CROSSWALK)

    assert gaz["OS12G0559400"] == "ncbigene:4352133"
    assert "ncbigene:9999999" not in gaz.values()


def test_build_gazetteer_skips_rows_that_do_not_resolve(tmp_path):
    fixture = "\n".join(["\t".join(_COLUMNS), _row(**{"CGSNL Gene Symbol": "[CMS-54257]"})]) + "\n"
    assert build_gazetteer(_write(tmp_path, fixture), _CROSSWALK) == {}


def test_build_gazetteer_reads_mislabeled_utf8_bom_file(tmp_path):
    gaz = build_gazetteer(_write(tmp_path), _CROSSWALK)
    assert gaz, "a BOM leaking into the first header name would empty the gazetteer"


# --- EntityMention shape -----------------------------------------------------------


def test_extract_mentions_returns_gene_entity_mentions_with_the_matched_form():
    gaz = {"GHD7": "ncbigene:4342860"}
    mentions = extract_mentions("GHD7 controls heading date", None, gaz)

    assert mentions == [EntityMention(vertex_type="Gene", entity_id="ncbigene:4342860", name="GHD7")]


def test_extract_mentions_searches_title_and_abstract_together():
    gaz = {"GHD7": "ncbigene:1", "XA21": "ncbigene:2"}
    mentions = extract_mentions("GHD7 study", "we also examined XA21", gaz)

    assert {m.entity_id for m in mentions} == {"ncbigene:1", "ncbigene:2"}


def test_extract_mentions_empty_when_nothing_matches():
    assert extract_mentions("a paper about soil", "no genes here", {"GHD7": "x"}) == []


def test_is_unambiguous_rejects_units_with_negative_exponents():
    """Rice papers are full of "µg mL-1", "mg kg-1", "t ha-1". ML-1 is a genuine Oryzabase
    gene symbol, but on a full-corpus dry run it was the 3rd most-matched form at 154 hits
    and every sampled occurrence was the concentration unit."""
    for unit in ("ML-1", "KG-1", "MG-1", "HA-1", "L-1", "H-1", "M-2", "S-1", "MOL-1", "KDA-1"):
        assert not is_admissible(unit, include_unaudited=True), unit


def test_is_unambiguous_keeps_gene_symbols_with_a_longer_hyphenated_stem():
    """The unit rule must not swallow real symbols -- BADH-2 is the rice fragrance gene."""
    for symbol in ("BADH-2", "AWPM-19", "CCOAOMT-1", "ALDC-1", "CDKA-1"):
        assert is_admissible(symbol, include_unaudited=True), symbol


def test_find_gene_mentions_does_not_fire_on_a_concentration_unit():
    """End-to-end guard: the unit must not produce a gene mention even when the gazetteer
    was built from a row whose symbol really is ML-1."""
    gaz = {f: "ncbigene:4342446" for f in ("ML-1", "OSML1") if is_admissible(f, include_unaudited=True)}
    assert find_gene_mentions("removed AFM1 from 50.0 ug mL-1 AFM1 solution", gaz) == {}
    assert find_gene_mentions("OsML1 was upregulated", gaz) == {"ncbigene:4342446": "OSML1"}


def test_is_unambiguous_rejects_domains_families_and_cross_species_symbols():
    """Audited from the 120 most-matched forms on a full-corpus dry run. WD40 is a repeat
    domain, R2R3-MYB a TF family, GA20 a hormone; NPR1/BRI1/BZR1 are canonically
    Arabidopsis and rice uses Os-prefixed names this gazetteer matches separately."""
    for form in ("WD40", "R2R3-MYB", "SNRK2", "AMT1", "GA20", "ATP6", "HSP70", "NPR1", "BRI1", "SOS1"):
        assert not is_admissible(form, include_unaudited=True), form


def test_is_unambiguous_keeps_the_rice_specific_symbols_from_the_same_audit():
    """The blocklist must not over-reach: these were audited as correct in the same pass."""
    for form in ("HD3A", "XA21", "EHD1", "GHD7", "RFT1", "SLR1", "SUB1", "DEP1", "BADH2", "NAL1", "IPA1"):
        assert is_admissible(form), form


def test_os_prefixed_orthologs_survive_when_the_bare_symbol_is_blocked():
    """Blocking bare NPR1 must not cost us OsNPR1 -- that's what makes the blocklist cheap."""
    assert not is_admissible("NPR1")
    assert is_admissible("OSNPR1")
    assert is_admissible("OSBRI1")


# --- the conservative default policy -----------------------------------------------


def test_is_rice_specific_covers_locus_ids_and_os_prefixed_symbols():
    assert is_rice_specific("OS01G0194300")
    assert is_rice_specific("LOC_OS01G05060")
    assert is_rice_specific("OSNRAMP5")
    assert not is_rice_specific("GHD7")
    assert not is_rice_specific("OSH1"), "4 chars -- too short to rely on the Os convention"


def test_default_policy_admits_audited_symbols_but_not_unaudited_lookalikes():
    """GHD7 and WD40 are structurally identical; only the audit separates them, so under the
    default policy an unaudited letters+digit symbol must be rejected."""
    assert is_admissible("GHD7")
    assert is_admissible("XA21")
    assert not is_admissible("WD40")
    assert not is_admissible("ZZZ9"), "a plausible-looking symbol nobody verified"


def test_default_policy_is_stricter_than_the_permissive_tier():
    assert not is_admissible("HSP70")
    assert is_admissible("HSP70", include_unaudited=False) is False
    # the permissive tier still blocks the audited rejects, but would admit unknown symbols
    assert is_admissible("ZZZ9", include_unaudited=True)


def test_build_gazetteer_default_excludes_unaudited_symbols(tmp_path):
    fixture = "\n".join(["\t".join(_COLUMNS), _row(**{"CGSNL Gene Symbol": "ZZZ9", "RAP ID": "Os12g0559400"})]) + "\n"
    gaz = build_gazetteer(_write(tmp_path, fixture), _CROSSWALK)

    assert "OS12G0559400" in gaz, "the locus id is always admissible"
    assert "ZZZ9" not in gaz
    assert "ZZZ9" in build_gazetteer(_write(tmp_path, fixture), _CROSSWALK, include_unaudited=True)


def test_backfill_gene_names_only_targets_unnamed_nodes(mocker):
    """GAF/Oryzabase create Gene nodes key-only, so trait queries return gene: null. The
    backfill must add a name without ever overwriting one another job wrote."""
    from spokebio.upsert import _BACKFILL_GENE_NAMES, backfill_gene_names

    mock_run_write = mocker.patch("spokebio.upsert.run_write", return_value=[{"named": 2}])
    assert backfill_gene_names({"ncbigene:1": "GHD7", "ncbigene:2": "XA21"}) == 2
    assert "WHERE n.name IS NULL" in _BACKFILL_GENE_NAMES
    assert mock_run_write.call_args.kwargs["genes"][0] == {"gene_id": "ncbigene:1", "name": "GHD7"}


def test_backfill_gene_names_noop_on_empty(mocker):
    from spokebio.upsert import backfill_gene_names

    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert backfill_gene_names({}) == 0
    mock_run_write.assert_not_called()


def test_upgrade_gene_names_overwrites_unlike_the_backfill(mocker):
    """The upgrade path must overwrite -- a gene named by the locus-id fallback on an earlier
    run would otherwise display Os08g0238500 forever, since backfill_gene_names fills only
    nulls. The safety guard is the caller's, so this SQL deliberately has no WHERE."""
    from spokebio.upsert import _UPGRADE_GENE_NAMES, upgrade_gene_names

    mock_run_write = mocker.patch("spokebio.upsert.run_write", return_value=[{"upgraded": 1}])

    assert upgrade_gene_names({"ncbigene:4345025": "DLH7"}) == 1
    assert "WHERE" not in _UPGRADE_GENE_NAMES
    assert mock_run_write.call_args.kwargs["genes"] == [{"gene_id": "ncbigene:4345025", "name": "DLH7"}]


def test_upgrade_gene_names_noop_on_empty(mocker):
    from spokebio.upsert import upgrade_gene_names

    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert upgrade_gene_names({}) == 0
    mock_run_write.assert_not_called()


def test_gene_name_backfill_only_upgrades_locus_id_names(mocker):
    """The load-bearing guard: a curated or extractor-assigned name must never be
    overwritten, and a locus id must never replace another locus id."""
    from spokebio import pipeline

    mocker.patch.object(pipeline, "ensure_gene_info_file", return_value="gene_info")
    mocker.patch.object(pipeline, "ensure_oryzabase_file", return_value="oryzabase")
    mocker.patch.object(pipeline, "build_locus_identifier_crosswalk", return_value={})
    mocker.patch.object(
        pipeline,
        "build_symbol_map",
        return_value={"already": "SD1", "locus": "DLH7", "named": "GHD7", "placeholder": "AMY2A"},
    )
    mocker.patch.object(
        pipeline,
        "build_gene_name_map",
        return_value={"missing": "Os01g0111100", "locus": "Os08g0238500", "named": "Os07g0261200"},
    )
    mocker.patch.object(
        pipeline,
        "read_gene_names",
        return_value={
            "already": "SD1",
            "locus": "Os08g0238500",
            "named": "OsWRKY45",
            "placeholder": "LOC4342055",
        },
    )
    fill = mocker.patch.object(pipeline, "backfill_gene_names", return_value=1)
    upgrade = mocker.patch.object(pipeline, "upgrade_gene_names", return_value=1)

    pipeline.run_gene_name_backfill()

    # "missing" has no stored name -> filled.
    assert fill.call_args.args[0] == {"missing": "Os01g0111100"}
    # "locus" holds a bare locus id and a curated symbol exists -> upgraded.
    # "already" holds a curated symbol -> untouched. "named" holds an extractor-assigned
    # symbol, not a locus id -> untouched despite a symbol being available.
    # "locus" holds a bare locus id, "placeholder" holds NCBI's LOC<id> -- both provisional.
    assert upgrade.call_args.args[0] == {"locus": "DLH7", "placeholder": "AMY2A"}


def test_backfill_gene_locus_ids_is_null_only(mocker):
    """locus_id is a stable fact, so a re-run must not thrash one already set -- and it is a
    secondary key, so this must never create or re-key a node."""
    from spokebio.upsert import _BACKFILL_GENE_LOCUS_IDS, backfill_gene_locus_ids

    mock_run_write = mocker.patch("spokebio.upsert.run_write", return_value=[{"assigned": 2}])

    assert backfill_gene_locus_ids({"ncbigene:1": "Os01g0100100", "ncbigene:2": "LOC_Os01g01010"}) == 2
    assert "WHERE n.locus_id IS NULL" in _BACKFILL_GENE_LOCUS_IDS
    assert "MERGE" not in _BACKFILL_GENE_LOCUS_IDS  # MATCH only: never creates a Gene
    assert mock_run_write.call_args.kwargs["genes"][0] == {"gene_id": "ncbigene:1", "locus_id": "Os01g0100100"}


def test_backfill_gene_locus_ids_noop_on_empty(mocker):
    from spokebio.upsert import backfill_gene_locus_ids

    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert backfill_gene_locus_ids({}) == 0
    mock_run_write.assert_not_called()


def test_find_genes_by_locus_id_keeps_ambiguous_hits(mocker):
    """103 rice locus ids map to more than one NCBI gene, so collapsing to a single value
    would silently pick an arbitrary one."""
    from spokebio.upsert import find_genes_by_locus_id

    mocker.patch(
        "spokebio.upsert.run_read",
        return_value=[
            {"locus_id": "Os03g0120900", "gene_id": "ncbigene:4324719"},
            {"locus_id": "Os03g0120900", "gene_id": "ncbigene:4331436"},
            {"locus_id": "Os01g0100100", "gene_id": "ncbigene:4326813"},
        ],
    )

    assert find_genes_by_locus_id(["Os03g0120900", "Os01g0100100"]) == {
        "Os03g0120900": ["ncbigene:4324719", "ncbigene:4331436"],
        "Os01g0100100": ["ncbigene:4326813"],
    }


def test_find_genes_by_locus_id_noop_on_empty(mocker):
    from spokebio.upsert import find_genes_by_locus_id

    mock_run_read = mocker.patch("spokebio.upsert.run_read")
    assert find_genes_by_locus_id([]) == {}
    mock_run_read.assert_not_called()


def test_secondary_key_index_is_notunique():
    """A UNIQUE index would reject the 103 rice locus ids that map to more than one gene.

    Asserts on the DDL the registry generates rather than on mocked calls -- importing
    spokebio.schema_ext is what registers the biology types.
    """
    from litgraph.db.registry import arcadedb_ddl, registry
    from spokebio import schema_ext  # noqa: F401  -- imported for its registration side effect

    statements = list(arcadedb_ddl(registry, embedding_dimensions=768))
    assert "CREATE PROPERTY Gene.locus_id STRING" in statements
    assert "CREATE INDEX ON Gene (locus_id) NOTUNIQUE" in statements
    assert "CREATE INDEX ON Gene (locus_id) UNIQUE" not in statements
    # gene_id remains the one canonical unique key.
    assert "CREATE INDEX ON Gene (gene_id) UNIQUE" in statements
