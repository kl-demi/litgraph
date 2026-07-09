from datetime import UTC, datetime
from litgraph.ingest.pipeline import fetch_new_papers

def test_fetch_new_papers():
  beginning_of_year = datetime(2026, 1, 1, tzinfo=UTC)

  papers = fetch_new_papers(
    categories=["cs.AI"],
    since=beginning_of_year,
    max_results=5
  )
  total = 0
  for paper in papers:
    assert paper.arxiv_id is not None
    assert paper.title is not None
    if paper.published_date is not None:
      assert paper.published_date >= beginning_of_year.date()
    total += 1

  assert total <= 5
  