"""One-off backfill for papers whose AUTHORED edges were lost to the FOREACH/MERGE
bug (see graph/upsert.py). Re-fetches each arxiv-sourced paper by ID from the arXiv
API and re-runs the (now fixed) upsert, which repopulates authors, published_date,
etc. without touching anything else. Safe to re-run — upsert_papers is idempotent.

Goes through _embed_and_upsert (not upsert_papers directly): the freshly re-fetched
Paper objects have embedding=None, and upsert_papers unconditionally SETs whatever
embedding it's given — calling it directly would silently wipe existing embeddings.
"""

import arxiv

from litgraph.db.neo4j_client import chunked, run_read
from litgraph.ingest.arxiv_source import _result_to_paper
from litgraph.ingest.pipeline import _embed_and_upsert

_MISSING_AUTHORS = """
MATCH (p:Paper)
WHERE p.source = 'arxiv' AND NOT (p)<-[:AUTHORED]-()
RETURN p.arxiv_id AS arxiv_id
"""


def main() -> None:
    arxiv_ids = [row["arxiv_id"] for row in run_read(_MISSING_AUTHORS)]
    print(f"{len(arxiv_ids)} papers missing authors.")

    client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
    total = 0
    for batch in chunked(arxiv_ids, 50):
        search = arxiv.Search(id_list=batch)
        papers = [_result_to_paper(result) for result in client.results(search)]
        _embed_and_upsert(papers)
        total += len(papers)
        print(f"  backfilled {total}/{len(arxiv_ids)}")

    print(f"Done. Backfilled {total} papers.")


if __name__ == "__main__":
    main()
