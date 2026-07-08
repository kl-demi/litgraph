"""
End-to-end test of the ingestion pipeline and all three search paths.

Exercises run_backload() and run_enrichment() against a real (local) Neo4j
instance and the real embedding model, and run_daily_fetch() against the
live arXiv API. The Semantic Scholar HTTP call is faked so citation data is
small and deterministic (the real "Attention Is All You Need" has tens of
thousands of citations, which would flood the graph). Checkpoint I/O is
faked too, so this test can't clobber the shared "arxiv_daily" checkpoint
used by real runs of the pipeline.

Requires: a reachable Neo4j instance (see docker-compose.yml / .env) and
network access to arxiv.org.
"""

import json
from datetime import UTC, datetime

from arxiv_graphdb.db.neo4j_client import run_write
from arxiv_graphdb.ingest import pipeline
from arxiv_graphdb.ingest.pipeline import run_backload, run_daily_fetch, run_enrichment
from arxiv_graphdb.ingest.semantic_scholar import SemanticScholarClient
from arxiv_graphdb.search.citations import get_citing_papers, get_references
from arxiv_graphdb.search.keyword import keyword_search
from arxiv_graphdb.search.semantic import semantic_search

ATTENTION_ID = "1706.03762"
BERT_ID = "1810.04805"
STUB_CITER_ID = "1911.02150"

_RECORDS = [
    {
        "id": ATTENTION_ID,
        "title": "Attention Is All You Need",
        "abstract": (
            "The dominant sequence transduction models are based on complex recurrent or "
            "convolutional neural networks. We propose a new simple network architecture, "
            "the Transformer, based solely on attention mechanisms, dispensing with "
            "recurrence and convolutions entirely."
        ),
        "categories": "cs.CL cs.LG",
        "authors_parsed": [["Vaswani", "Ashish", ""]],
        "versions": [{"created": "Mon, 12 Jun 2017 17:57:34 GMT"}],
        "update_date": "2017-06-12",
        "doi": None,
        "journal-ref": None,
        "comments": None,
    },
    {
        "id": BERT_ID,
        "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "abstract": (
            "We introduce a new language representation model called BERT. Unlike recent "
            "language representation models, BERT is designed to pretrain deep "
            "bidirectional representations by jointly conditioning on both left and right "
            "context in all layers."
        ),
        "categories": "cs.CL",
        "authors_parsed": [["Devlin", "Jacob", ""]],
        "versions": [{"created": "Thu, 11 Oct 2018 00:50:01 GMT"}],
        "update_date": "2019-05-24",
        "doi": None,
        "journal-ref": None,
        "comments": None,
    },
]

# Fake Semantic Scholar responses: Attention cites BERT, and a stub paper cites Attention.
_S2_PAYLOAD = {
    ATTENTION_ID: {
        "paperId": "s2-attention",
        "externalIds": {"ArXiv": ATTENTION_ID},
        "citationCount": 50000,
        "referenceCount": 1,
        "influentialCitationCount": 9000,
        "references": [
            {
                "paperId": "s2-bert",
                "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                "externalIds": {"ArXiv": BERT_ID},
            }
        ],
        "citations": [
            {
                "paperId": "s2-fastformer",
                "title": "Fast Transformer Decoding: One Write-Head is All You Need",
                "externalIds": {"ArXiv": STUB_CITER_ID},
            }
        ],
    },
    BERT_ID: {
        "paperId": "s2-bert",
        "externalIds": {"ArXiv": BERT_ID},
        "citationCount": 90000,
        "referenceCount": 0,
        "influentialCitationCount": 0,
        "references": [],
        "citations": [],
    },
}


def _delete_test_papers():
    run_write(
        "MATCH (p:Paper) WHERE p.arxiv_id IN $ids DETACH DELETE p",
        ids=[ATTENTION_ID, BERT_ID, STUB_CITER_ID],
    )


def _write_snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("\n".join(json.dumps(r) for r in _RECORDS))
    return path


def test_pipeline_end_to_end(tmp_path, mocker):
    _delete_test_papers()
    try:
        snapshot = _write_snapshot(tmp_path)
        ingested = run_backload(snapshot, categories=["cs"])
        assert ingested == 2

        mocker.patch.object(
            SemanticScholarClient,
            "_post_batch",
            side_effect=lambda ids: [_S2_PAYLOAD.get(i) for i in ids],
        )
        enriched = run_enrichment(limit=10)
        assert enriched == 2

        # Fake the checkpoint so this can't read or overwrite the real "arxiv_daily" job.
        mocker.patch.object(pipeline, "get_checkpoint", return_value=datetime.now(UTC))
        mock_set_checkpoint = mocker.patch.object(pipeline, "set_checkpoint")
        fetched = run_daily_fetch(["cs.CL"])
        assert fetched == 0
        mock_set_checkpoint.assert_not_called()

        # 1. Semantic search: nearest-neighbor over embeddings, no shared keywords with the query.
        semantic_hits = semantic_search("neural architecture using self-attention instead of recurrence", top_k=5)
        assert ATTENTION_ID in {r["arxiv_id"] for r in semantic_hits}

        # 2. Keyword search: full-text match on a term unique to one abstract.
        keyword_hits = keyword_search("bidirectional", top_k=5)
        assert BERT_ID in {r["arxiv_id"] for r in keyword_hits}

        # 3. Citation graph: edges written by run_enrichment, in both directions.
        references = get_references(ATTENTION_ID)
        assert BERT_ID in {r["arxiv_id"] for r in references}

        citing = get_citing_papers(ATTENTION_ID)
        citer = next(r for r in citing if r["arxiv_id"] == STUB_CITER_ID)
        assert citer["is_stub"] is True
    finally:
        _delete_test_papers()
