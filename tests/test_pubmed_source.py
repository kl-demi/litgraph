from datetime import UTC, datetime

from litgraph.ingest.pubmed_source import fetch_new_papers

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


def test_fetch_new_papers_empty_when_no_results(mocker):
    fake_client = FakeClient(esearch_ids=[], efetch_content=_EFETCH_XML)
    mocker.patch("litgraph.ingest.pubmed_source.httpx.Client", return_value=fake_client)

    papers = list(fetch_new_papers('"Anatomy"[MeSH Major Topic]'))
    assert papers == []
