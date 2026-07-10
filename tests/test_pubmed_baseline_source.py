from datetime import date

from litgraph.ingest.pubmed_baseline_source import iter_pubmed_baseline_papers

_ARTICLE_ANATOMY = """
<PubmedArticle>
  <MedlineCitation>
    <PMID>12345678</PMID>
    <DateRevised><Year>2021</Year><Month>02</Month><Day>01</Day></DateRevised>
    <Article>
      <Journal>
        <ISOAbbreviation>J Anat</ISOAbbreviation>
        <JournalIssue><Volume>10</Volume><Issue>2</Issue>
          <PubDate><Year>2021</Year><Month>Jan</Month><Day>04</Day></PubDate>
        </JournalIssue>
      </Journal>
      <ArticleTitle>A Great Paper About Anatomy</ArticleTitle>
      <Pagination><MedlinePgn>100-110</MedlinePgn></Pagination>
      <ELocationID EIdType="doi">10.1000/anat.1</ELocationID>
      <Abstract><AbstractText>This is the abstract.</AbstractText></Abstract>
      <AuthorList>
        <Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author>
        <Author><LastName>Smith</LastName><ForeName>John</ForeName></Author>
      </AuthorList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName MajorTopicYN="Y">Anatomy</DescriptorName></MeshHeading>
      <MeshHeading><DescriptorName MajorTopicYN="N">Humans</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
</PubmedArticle>
"""

_ARTICLE_UNRELATED = """
<PubmedArticle>
  <MedlineCitation>
    <PMID>87654321</PMID>
    <Article>
      <Journal><JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue></Journal>
      <ArticleTitle>A Paper About Cardiology</ArticleTitle>
      <Abstract><AbstractText>Unrelated abstract.</AbstractText></Abstract>
      <AuthorList></AuthorList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName MajorTopicYN="Y">Cardiology</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
</PubmedArticle>
"""


def _write_xml(tmp_path, articles):
    path = tmp_path / "pubmed21n0001.xml"
    body = "\n".join(articles)
    path.write_text(f"<PubmedArticleSet>{body}</PubmedArticleSet>")
    return path


def test_parses_basic_fields(tmp_path):
    path = _write_xml(tmp_path, [_ARTICLE_ANATOMY])
    papers = list(iter_pubmed_baseline_papers(path))
    assert len(papers) == 1
    paper = papers[0]
    assert paper.pmid == "12345678"
    assert paper.title == "A Great Paper About Anatomy"
    assert paper.abstract == "This is the abstract."
    assert paper.authors == ["Jane Doe", "John Smith"]
    assert paper.categories == ["Anatomy", "Humans"]
    assert paper.primary_category == "Anatomy"
    assert paper.published_date == date(2021, 1, 4)
    assert paper.updated_date == date(2021, 2, 1)
    assert paper.doi == "10.1000/anat.1"
    assert paper.journal_ref == "J Anat. 2021;10(2):100-110"
    assert paper.source == "pubmed_baseline"


def test_filters_by_mesh_terms(tmp_path):
    path = _write_xml(tmp_path, [_ARTICLE_ANATOMY, _ARTICLE_UNRELATED])
    papers = list(iter_pubmed_baseline_papers(path, mesh_terms=["Anatomy"]))
    assert [p.pmid for p in papers] == ["12345678"]


def test_filters_by_date_range(tmp_path):
    path = _write_xml(tmp_path, [_ARTICLE_ANATOMY])
    assert list(iter_pubmed_baseline_papers(path, start_date=date(2021, 1, 5))) == []
    assert len(list(iter_pubmed_baseline_papers(path, start_date=date(2021, 1, 1)))) == 1
    assert list(iter_pubmed_baseline_papers(path, end_date=date(2021, 1, 1))) == []


def test_respects_limit(tmp_path):
    articles = [_ARTICLE_ANATOMY.replace("12345678", f"1234567{i}") for i in range(5)]
    path = _write_xml(tmp_path, articles)
    papers = list(iter_pubmed_baseline_papers(path, limit=2))
    assert len(papers) == 2
