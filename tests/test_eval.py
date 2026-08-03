import csv

from spokebio.eval import (
    STRATUM_CONSERVATIVE,
    STRATUM_NO_MATCH,
    STRATUM_PERMISSIVE_ONLY,
    build_candidates,
    sample,
    score_precision,
    score_recall,
    sentence_containing,
    stratify,
    write_precision_worksheet,
    write_recall_worksheet,
)

_CONSERVATIVE = {"GHD7": "ncbigene:1", "OSNRAMP5": "ncbigene:2"}
_PERMISSIVE = {**_CONSERVATIVE, "ZZZ9": "ncbigene:3"}

_PAPERS = [
    {"id": "p1", "pmid": "111", "title": "GHD7 controls heading date", "abstract": "We show GHD7 acts early."},
    {"id": "p2", "pmid": "222", "title": "A ZZZ9 study", "abstract": "ZZZ9 was upregulated."},
    {"id": "p3", "pmid": "333", "title": "Soil chemistry", "abstract": "No genes are discussed here."},
]


# --- sentence context --------------------------------------------------------------


def test_sentence_containing_returns_the_sentence_with_the_match():
    text = "Rice yields vary. GHD7 controls heading date. Other factors matter too."
    assert sentence_containing(text, "GHD7") == "GHD7 controls heading date."


def test_sentence_containing_is_case_insensitive_and_boundary_aware():
    assert sentence_containing("We studied Ghd7 closely.", "GHD7") == "We studied Ghd7 closely."
    assert sentence_containing("The AGHD7X protein.", "GHD7") == ""


def test_sentence_containing_falls_back_to_a_window_without_sentence_breaks():
    text = "no sentence breaks here at all GHD7 just one long run of words " * 6
    out = sentence_containing(text, "GHD7")
    assert "GHD7" in out and len(out) <= 300


def test_sentence_containing_truncates_a_very_long_sentence():
    text = "GHD7 " + "padding words " * 200 + "."
    out = sentence_containing(text, "GHD7")
    assert out.endswith("...") and len(out) <= 300


def test_sentence_containing_handles_missing_text():
    assert sentence_containing("", "GHD7") == ""
    assert sentence_containing(None, "GHD7") == ""


# --- stratification ---------------------------------------------------------------


def test_stratify_buckets_by_which_tier_matched():
    buckets = stratify(_PAPERS, _CONSERVATIVE, _PERMISSIVE)

    assert [p["id"] for p in buckets[STRATUM_CONSERVATIVE]] == ["p1"]
    assert [p["id"] for p in buckets[STRATUM_PERMISSIVE_ONLY]] == ["p2"]
    assert [p["id"] for p in buckets[STRATUM_NO_MATCH]] == ["p3"]


def test_build_candidates_excludes_matches_the_narrower_gazetteer_already_found():
    """permissive_only rows must be the *incremental* matches, not a re-audit of verified
    forms -- otherwise the tier's measured precision is diluted by known-good rows."""
    paper = {"id": "p4", "pmid": "444", "title": "GHD7 and ZZZ9", "abstract": ""}

    candidates = build_candidates([paper], _PERMISSIVE, STRATUM_PERMISSIVE_ONLY, exclude=_CONSERVATIVE)

    assert [c.gene_id for c in candidates] == ["ncbigene:3"]


def test_build_candidates_attaches_the_justifying_sentence():
    candidates = build_candidates(_PAPERS[:1], _CONSERVATIVE, STRATUM_CONSERVATIVE)

    assert len(candidates) == 1
    assert candidates[0].matched_form == "GHD7"
    assert "GHD7" in candidates[0].sentence


def test_sample_is_deterministic_for_a_seed():
    items = list(range(100))
    assert sample(items, 10, seed=7) == sample(items, 10, seed=7)
    assert sample(items, 10, seed=7) != sample(items, 10, seed=8)


def test_sample_returns_everything_when_n_exceeds_population():
    assert sorted(sample([1, 2, 3], 10, seed=1)) == [1, 2, 3]


# --- worksheets -------------------------------------------------------------------


def test_write_precision_worksheet_leaves_the_verdict_blank(tmp_path):
    path = tmp_path / "precision.csv"
    candidates = build_candidates(_PAPERS[:1], _CONSERVATIVE, STRATUM_CONSERVATIVE)

    assert write_precision_worksheet(path, candidates, {"ncbigene:1": "GHD7"}) == 1
    row = next(iter(csv.DictReader(open(path, encoding="utf-8"))))
    assert row["correct"] == ""
    assert row["gene_symbol"] == "GHD7"
    assert row["tier"] == STRATUM_CONSERVATIVE


def test_write_recall_worksheet_prefills_what_the_gazetteer_found(tmp_path):
    path = tmp_path / "recall.csv"

    assert write_recall_worksheet(path, _PAPERS, _CONSERVATIVE) == 3
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows[0]["found_by_gazetteer"] == "GHD7"
    assert rows[2]["found_by_gazetteer"] == ""
    assert all(r["missed_genes"] == "" for r in rows)


# --- scoring ----------------------------------------------------------------------


def test_score_precision_reports_per_tier():
    rows = [
        {"tier": "conservative", "correct": "y"},
        {"tier": "conservative", "correct": "y"},
        {"tier": "conservative", "correct": "n"},
        {"tier": "permissive_only", "correct": "n"},
        {"tier": "permissive_only", "correct": "y"},
    ]
    scored = score_precision(rows)

    assert scored["conservative"]["precision"] == 2 / 3
    assert scored["permissive_only"]["precision"] == 0.5


def test_score_precision_counts_unlabelled_rows_as_skipped_not_as_a_verdict():
    scored = score_precision([{"tier": "conservative", "correct": ""}, {"tier": "conservative", "correct": "y"}])

    assert scored["conservative"]["skipped"] == 1
    assert scored["conservative"]["labelled"] == 1
    assert scored["conservative"]["precision"] == 1.0


def test_score_precision_accepts_common_verdict_spellings():
    scored = score_precision(
        [
            {"tier": "t", "correct": "Y"},
            {"tier": "t", "correct": "yes"},
            {"tier": "t", "correct": "N"},
            {"tier": "t", "correct": "no"},
        ]
    )
    assert scored["t"]["correct"] == 2 and scored["t"]["wrong"] == 2


def test_score_precision_on_an_empty_worksheet_does_not_divide_by_zero():
    assert score_precision([]) == {}
    assert score_precision([{"tier": "t", "correct": ""}])["t"]["precision"] == 0.0


def test_score_recall_counts_missed_mentions():
    rows = [
        {"found_by_gazetteer": "GHD7, XA21", "missed_genes": "OsNPR1", "notes": ""},
        {"found_by_gazetteer": "", "missed_genes": "", "notes": "reviewed, nothing missed"},
    ]
    r = score_recall(rows)

    assert r["mentions_found"] == 2
    assert r["mentions_missed"] == 1
    assert r["papers_reviewed"] == 2
    assert r["recall"] == 2 / 3


def test_score_recall_treats_a_wholly_blank_row_as_unreviewed():
    r = score_recall([{"found_by_gazetteer": "", "missed_genes": "", "notes": ""}])

    assert r["papers_skipped"] == 1
    assert r["papers_reviewed"] == 0
    assert r["recall"] == 0.0


def test_display_symbols_prefers_a_readable_symbol_over_a_locus_id():
    """The worksheet's gene_symbol column exists so the annotator can recognise the gene; a
    plain dict inversion yields the locus id, which defeats that."""
    from spokebio.eval import display_symbols

    gaz = {"OS06G0579200": "ncbigene:9", "OSLCT1": "ncbigene:9", "LOC_OS06G38120": "ncbigene:9"}
    assert display_symbols(gaz)["ncbigene:9"] == "OSLCT1"


def test_display_symbols_falls_back_to_a_locus_id_when_thats_all_there_is():
    from spokebio.eval import display_symbols

    assert display_symbols({"OS06G0579200": "ncbigene:9"})["ncbigene:9"] == "OS06G0579200"


def test_has_usable_abstract_rejects_blank_and_stub_abstracts():
    from spokebio.eval import has_usable_abstract

    assert not has_usable_abstract({"abstract": None})
    assert not has_usable_abstract({"abstract": "   "})
    assert not has_usable_abstract({"abstract": "too short"})
    assert has_usable_abstract({"abstract": "x" * 100})


def test_score_recall_reports_nothing_reviewed_on_an_untouched_worksheet():
    """Regression: `found_by_gazetteer` is pre-filled by the builder, so counting it as
    evidence of review made a freshly-generated sheet score 100% recall."""
    fresh = [
        {"found_by_gazetteer": "GHD7, XA21", "missed_genes": "", "notes": ""},
        {"found_by_gazetteer": "", "missed_genes": "", "notes": ""},
    ]
    r = score_recall(fresh)

    assert r["papers_reviewed"] == 0
    assert r["papers_skipped"] == 2
    assert r["mentions_found"] == 0
    assert r["recall"] == 0.0
