from spokebio.extract import _find_unchecked_query, run_extraction
from spokebio.models import EntityMention
from spokebio.upsert import MENTION_STAT_KEYS


class FakeExtractor:
    name = "fake"
    requires = ("pmid",)

    def __init__(self, results):
        self._results = results
        self.seen_papers = None

    def extract(self, papers):
        self.seen_papers = papers
        yield from self._results


def _mention(entity_id="ncbigene:1"):
    return EntityMention(vertex_type="Gene", entity_id=entity_id, name="A")


def _stats(**overrides):
    """Mirrors the real upsert_mentions return shape via MENTION_STAT_KEYS, so this
    fixture can't drift out of sync with it the way the hardcoded copy once did."""
    stats = {key: 0 for key in MENTION_STAT_KEYS}
    stats.update(overrides)
    return stats


def test_candidate_query_filters_on_required_fields():
    query = _find_unchecked_query(("pmid", "abstract"))
    assert "AND p.pmid IS NOT NULL AND p.abstract IS NOT NULL" in query
    assert "p.pmid AS pmid, p.abstract AS abstract" in query
    assert "ExtractionChecked {extractor: $extractor, paper_id: p.id}" in query


def test_run_extraction_totals_carry_every_upsert_mentions_stat(mocker):
    """Regression test for a real bug: run_extraction once hardcoded its own key list,
    so a stat upsert_mentions returned (new_diseases) was silently dropped and a caller
    reading totals['new_diseases'] hit a KeyError. Asserting the key set stays derived
    from MENTION_STAT_KEYS, not re-listed, is what would have caught it."""
    mocker.patch("spokebio.extract.run_read", return_value=[{"id": "pmid:1", "pmid": "1"}])
    mocker.patch("spokebio.extract.upsert_mentions", return_value=_stats())
    mocker.patch("spokebio.extract.mark_papers_checked")

    totals = run_extraction(FakeExtractor([("pmid:1", [_mention()])]))

    assert set(totals) == {"papers_processed", *MENTION_STAT_KEYS}


def test_run_extraction_upserts_with_the_extractor_as_source(mocker):
    mocker.patch("spokebio.extract.run_read", return_value=[{"id": "pmid:1", "pmid": "1"}])
    mock_upsert = mocker.patch("spokebio.extract.upsert_mentions", return_value=_stats(new_genes=1))
    mock_checked = mocker.patch("spokebio.extract.mark_papers_checked")

    totals = run_extraction(FakeExtractor([("pmid:1", [_mention()])]))

    assert totals["papers_processed"] == 1
    assert totals["new_genes"] == 1
    assert mock_upsert.call_args.kwargs["source"] == "fake"
    assert mock_checked.call_args.args[0] == "fake"
    assert mock_checked.call_args.args[1] == ["pmid:1"]


def test_run_extraction_passes_candidate_rows_to_the_extractor(mocker):
    rows = [{"id": "pmid:1", "pmid": "1"}, {"id": "pmid:2", "pmid": "2"}]
    mocker.patch("spokebio.extract.run_read", return_value=rows)
    mocker.patch("spokebio.extract.upsert_mentions", return_value=_stats())
    mocker.patch("spokebio.extract.mark_papers_checked")

    extractor = FakeExtractor([("pmid:1", []), ("pmid:2", [])])
    run_extraction(extractor)

    assert extractor.seen_papers == rows


def test_run_extraction_marks_never_yielded_papers_checked(mocker):
    """A paper the extractor never yields (e.g. a PMID PubTator3 silently omits) must
    still be marked checked, or it reappears at the front of every future run."""
    rows = [{"id": "pmid:1", "pmid": "1"}, {"id": "pmid:2", "pmid": "2"}]
    mocker.patch("spokebio.extract.run_read", return_value=rows)
    mocker.patch("spokebio.extract.upsert_mentions", return_value=_stats())
    mock_checked = mocker.patch("spokebio.extract.mark_papers_checked")

    totals = run_extraction(FakeExtractor([("pmid:1", [_mention()])]))

    assert totals["papers_processed"] == 2
    checked_ids = [ids for _, ids, _ in (c.args for c in mock_checked.call_args_list)]
    assert ["pmid:1"] in checked_ids
    assert ["pmid:2"] in checked_ids


def test_run_extraction_ignores_papers_outside_the_candidate_set(mocker):
    mocker.patch("spokebio.extract.run_read", return_value=[{"id": "pmid:1", "pmid": "1"}])
    mock_upsert = mocker.patch("spokebio.extract.upsert_mentions", return_value=_stats())
    mocker.patch("spokebio.extract.mark_papers_checked")

    run_extraction(FakeExtractor([("pmid:999", [_mention()]), ("pmid:1", [])]))

    assert list(mock_upsert.call_args.args[0]) == ["pmid:1"]


def test_run_extraction_noop_when_everything_is_checked(mocker):
    mocker.patch("spokebio.extract.run_read", return_value=[])
    mock_upsert = mocker.patch("spokebio.extract.upsert_mentions")

    totals = run_extraction(FakeExtractor([]))

    assert totals["papers_processed"] == 0
    mock_upsert.assert_not_called()


def test_run_extraction_flushes_in_batches(mocker):
    rows = [{"id": f"pmid:{i}", "pmid": str(i)} for i in range(150)]
    mocker.patch("spokebio.extract.run_read", return_value=rows)
    mock_upsert = mocker.patch("spokebio.extract.upsert_mentions", return_value=_stats())
    mocker.patch("spokebio.extract.mark_papers_checked")

    run_extraction(FakeExtractor([(f"pmid:{i}", []) for i in range(150)]))

    batch_sizes = [len(c.args[0]) for c in mock_upsert.call_args_list]
    assert batch_sizes == [100, 50]
