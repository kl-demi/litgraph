"""One-off fix for embeddings wiped by an earlier run of backfill_authors.py: that
script called upsert_papers() directly on freshly re-fetched Paper objects (which
have embedding=None), and upsert_papers unconditionally SETs whatever embedding
it's given, silently clearing the previously computed ones. Re-fetches each
affected arxiv-sourced paper and re-embeds it via _embed_and_upsert. Safe to
re-run — idempotent.
"""

import arxiv

from litgraph.db.neo4j_client import chunked, run_read
from litgraph.ingest.arxiv_source import _result_to_paper
from litgraph.ingest.pipeline import _embed_and_upsert

_MISSING_EMBEDDING = """
MATCH (p:Paper)
WHERE p.source = 'arxiv' AND p.is_stub = false AND p.embedding IS NULL
RETURN p.arxiv_id AS arxiv_id
"""


def main() -> None:
    arxiv_ids = [row["arxiv_id"] for row in run_read(_MISSING_EMBEDDING)]
    print(f"{len(arxiv_ids)} papers missing embeddings.")

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
