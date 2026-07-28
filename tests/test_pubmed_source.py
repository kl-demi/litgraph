from datetime import UTC, date, datetime, timedelta

from litgraph.ingest.pubmed_source import fetch_historical_papers, fetch_new_papers, iter_date_windows


def _article_fragment(pmid: str) -> str:
    return f"""<PubmedArticle>
  <MedlineCitation>
    <PMID>{pmid}</PMID>
    <Article>
      <Journal><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>
      <ArticleTitle>A Paper {pmid}</ArticleTitle>
      <Abstract><AbstractText>Abstract {pmid}.</AbstractText></Abstract>
      <AuthorList></AuthorList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName MajorTopicYN="Y">Anatomy</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
</PubmedArticle>"""


def _article_xml(*pmids: str) -> str:
    return "<PubmedArticleSet>" + "".join(_article_fragment(p) for p in pmids) + "</PubmedArticleSet>"

_EFETCH_XML = """<PubmedArticleSet>
<PubmedArticle>
  <MedlineCitation>
    <PMID>12345678</PMID>
    <Article>
      <Journal><JournalIssue><PubDate><Year>2026</Year><Month>Jan</Month><Day>02</Day></PubDate></JournalIssue></Journal>
      <ArticleTitle>A Great Paper About Anatomy</ArticleTitle>
      <Abstract><AbstractText>This is the abstract.</AbstractText></Abstract>
      <AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName MajorTopicYN="Y">Anatomy</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""


class FakeResponse:
    def __init__(self, payload=None, content=None):
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, esearch_ids, efetch_content):
        self._esearch_ids = esearch_ids
        self._efetch_content = efetch_content
        self.get_calls = []
        self.post_calls = []

    def get(self, path, params=None):
        self.get_calls.append((path, params))
        return FakeResponse(payload={"esearchresult": {"idlist": self._esearch_ids}})

    def post(self, path, params=None, data=None):
        self.post_calls.append((path, params, data))
        return FakeResponse(content=self._efetch_content.encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def test_fetch_new_papers_parses_and_paginates(mocker):
    fake_client = FakeClient(esearch_ids=["12345678"], efetch_content=_EFETCH_XML)
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)

    since = datetime(2026, 1, 1, tzinfo=UTC)
    papers = list(fetch_new_papers('"Anatomy"[MeSH Major Topic]', since=since))

    assert len(papers) == 1
    paper = papers[0]
    assert paper.pmid == "12345678"
    assert paper.title == "A Great Paper About Anatomy"
    assert paper.source == "pubmed"
    assert paper.categories == ["Anatomy"]

    esearch_params = fake_client.get_calls[0][1]
    assert esearch_params["term"] == '"Anatomy"[MeSH Major Topic]'
    assert esearch_params["mindate"] == "2026/01/01"
    assert esearch_params["sort"] == "pub_date"


def test_fetch_new_papers_empty_when_no_results(mocker):
    fake_client = FakeClient(esearch_ids=[], efetch_content=_EFETCH_XML)
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)

    papers = list(fetch_new_papers('"Anatomy"[MeSH Major Topic]'))
    assert papers == []


class FakeHistoryClient:
    """Fakes the usehistory=y esearch + WebEnv/query_key/retstart efetch pagination path."""

    def __init__(self, count: int, batches_by_retstart: dict):
        self._count = count
        self._batches_by_retstart = batches_by_retstart
        self.get_calls = []
        self.post_calls = []

    def get(self, path, params=None):
        self.get_calls.append((path, params))
        return FakeResponse(
            payload={"esearchresult": {"webenv": "WE123", "querykey": "1", "count": str(self._count)}}
        )

    def post(self, path, params=None, data=None):
        self.post_calls.append((path, params, data))
        xml = self._batches_by_retstart[data["retstart"]]
        return FakeResponse(content=xml.encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def test_fetch_historical_papers_paginates_via_retstart(mocker):
    fake_client = FakeHistoryClient(
        count=3,
        batches_by_retstart={
            0: _article_xml("111", "222"),
            2: _article_xml("333"),
        },
    )
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    papers = list(
        fetch_historical_papers(
            '"Anatomy"[MeSH Major Topic]', start_date=date(2020, 1, 1), end_date=date(2020, 12, 31), batch_size=2
        )
    )

    assert [p.pmid for p in papers] == ["111", "222", "333"]

    esearch_params = fake_client.get_calls[0][1]
    assert esearch_params["usehistory"] == "y"
    assert esearch_params["mindate"] == "2020/01/01"
    assert esearch_params["maxdate"] == "2020/12/31"
    assert esearch_params["sort"] == "pub_date"

    retstarts = [call[2]["retstart"] for call in fake_client.post_calls]
    assert retstarts == [0, 2]
    for _, _, data in fake_client.post_calls:
        assert data["WebEnv"] == "WE123"
        assert data["query_key"] == "1"


def test_fetch_historical_papers_limit_is_window_aligned(mocker):
    """``limit`` is enforced at window boundaries, so a bounded run finishes the window
    it is in rather than cutting mid-stream. Stopping mid-window would leave no resumable
    boundary to record -- the failure mode that let the old date checkpoint re-ingest the
    same records forever (docs/known_bugs.md)."""
    fake_client = FakeHistoryClient(
        count=3,
        batches_by_retstart={
            0: _article_xml("111", "222"),
            2: _article_xml("333"),
        },
    )
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    papers = list(
        fetch_historical_papers(
            '"Anatomy"[MeSH Major Topic]',
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
            batch_size=2,
            limit=1,
        )
    )

    assert [p.pmid for p in papers] == ["111", "222", "333"]


def test_fetch_historical_papers_reports_resume_boundary_per_window(mocker):
    """The boundary handed back is the day *before* the window start, so a resuming run
    continues strictly below what has already been ingested -- no overlap, no gap."""
    fake_client = FakeHistoryClient(count=2, batches_by_retstart={0: _article_xml("111", "222")})
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    boundaries = []
    list(
        fetch_historical_papers(
            '"Anatomy"[MeSH Major Topic]',
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
            batch_size=2,
            on_window_complete=lambda resume_from, unreachable: boundaries.append((resume_from, unreachable)),
        )
    )

    assert boundaries == [(date(2019, 12, 31), 0)]


def test_fetch_historical_papers_splits_when_over_the_offset_limit(mocker):
    """A query above the efetch paging ceiling gets split into date windows instead of
    dying with a 400 at retstart ~10,000."""
    mocker.patch("litgraph.ingest.pubmed_source._HISTORY_OFFSET_LIMIT", 2)
    fake_client = FakeHistoryClient(count=4, batches_by_retstart={0: _article_xml("111", "222")})
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")
    # The full span is over the ceiling; each half lands on it, so it splits exactly once.
    mocker.patch(
        "litgraph.ingest.pubmed_source._esearch_count",
        side_effect=lambda client, terms, start, end: 4 if (end - start).days > 200 else 2,
    )

    papers = list(
        fetch_historical_papers(
            '"Anatomy"[MeSH Major Topic]',
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
            batch_size=2,
        )
    )

    # Two windows drained, so the same faked batch is yielded twice -- the point is that
    # it split at all rather than paging one oversized history set.
    assert len(papers) == 4
    assert len(fake_client.post_calls) == 2
    assert all(call[2]["retstart"] == 0 for call in fake_client.post_calls)


def test_fetch_historical_papers_caps_retstart_at_the_offset_limit(mocker):
    """An unsplittable single day bigger than the ceiling is truncated rather than paged
    into a 400, and the shortfall is reported to the caller."""
    mocker.patch("litgraph.ingest.pubmed_source._HISTORY_OFFSET_LIMIT", 2)
    fake_client = FakeHistoryClient(count=6, batches_by_retstart={0: _article_xml("111", "222")})
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")
    mocker.patch("litgraph.ingest.pubmed_source._esearch_count", return_value=6)

    shortfalls = []
    list(
        fetch_historical_papers(
            '"Anatomy"[MeSH Major Topic]',
            start_date=date(2020, 6, 1),
            end_date=date(2020, 6, 1),  # single day: cannot be split further
            batch_size=2,
            on_window_complete=lambda resume_from, unreachable: shortfalls.append(unreachable),
        )
    )

    assert [call[2]["retstart"] for call in fake_client.post_calls] == [0]
    assert shortfalls == [4]  # 6 matched, only 2 reachable


def test_fetch_historical_papers_empty_when_no_matches(mocker):
    fake_client = FakeHistoryClient(count=0, batches_by_retstart={})
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    papers = list(fetch_historical_papers('"Anatomy"[MeSH Major Topic]'))
    assert papers == []
    assert fake_client.post_calls == []


def test_iter_date_windows_yields_one_window_when_under_the_cap():
    windows = list(iter_date_windows(lambda s, e: 50, date(2020, 1, 1), date(2020, 12, 31), max_count=100))

    assert windows == [(date(2020, 1, 1), date(2020, 12, 31), 50)]


def test_iter_date_windows_skips_empty_subranges():
    """Cost should scale with how dense the corpus is, not with the span requested --
    an open-ended range starting in 1800 must not cost a walk over every empty century."""
    calls = []

    def count_fn(start, end):
        calls.append((start, end))
        return 0

    assert list(iter_date_windows(count_fn, date(1800, 1, 1), date(2026, 1, 1), max_count=10)) == []
    assert calls == [(date(1800, 1, 1), date(2026, 1, 1))]


def test_iter_date_windows_bisects_until_under_the_cap():
    # 40 records spread evenly over the span, so each halving halves the count.
    span_start, span_end = date(2020, 1, 1), date(2020, 12, 31)
    total_days = (span_end - span_start).days + 1

    def count_fn(start, end):
        return round(40 * ((end - start).days + 1) / total_days)

    windows = list(iter_date_windows(count_fn, span_start, span_end, max_count=10))

    assert len(windows) == 4
    assert all(count <= 10 for _, _, count in windows)


def test_iter_date_windows_walks_newest_first_and_covers_the_span_exactly():
    span_start, span_end = date(2020, 1, 1), date(2020, 12, 31)
    total_days = (span_end - span_start).days + 1

    windows = list(
        iter_date_windows(
            lambda s, e: round(40 * ((e - s).days + 1) / total_days), span_start, span_end, max_count=10
        )
    )

    # Newest window first.
    assert windows[0][1] == span_end
    assert windows[-1][0] == span_start
    # Contiguous, non-overlapping, and complete when read oldest-first.
    oldest_first = list(reversed(windows))
    assert oldest_first[0][0] == span_start
    assert oldest_first[-1][1] == span_end
    for earlier, later in zip(oldest_first, oldest_first[1:], strict=False):
        assert later[0] == earlier[1] + timedelta(days=1)


def test_iter_date_windows_yields_oversized_single_day_rather_than_looping():
    """A single day over the cap can't be split -- it must still be yielded, or the
    records in it become permanently unreachable."""
    windows = list(iter_date_windows(lambda s, e: 5000, date(2020, 6, 1), date(2020, 6, 1), max_count=10))

    assert windows == [(date(2020, 6, 1), date(2020, 6, 1), 5000)]
