from datetime import UTC, date, datetime

from litgraph.ingest.pubmed_source import fetch_historical_papers, fetch_new_papers


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


def test_fetch_historical_papers_empty_when_no_matches(mocker):
    fake_client = FakeHistoryClient(count=0, batches_by_retstart={})
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    papers = list(fetch_historical_papers('"Anatomy"[MeSH Major Topic]'))
    assert papers == []
    assert fake_client.post_calls == []
