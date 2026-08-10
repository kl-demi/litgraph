from datetime import date

import httpx

from litgraph.ingest import pipeline
from litgraph.models import Paper


def _paper(arxiv_id: str, published: date | None) -> Paper:
    return Paper(arxiv_id=arxiv_id, title="T", published_date=published)


def _mock_embed(mocker):
    """Returns one vector per text passed in, so it works across any batch size."""
    mocker.patch.object(pipeline, "embed_texts", side_effect=lambda texts: [[0.1]] * len(texts))


def test_consume_batches_at_the_given_size(mocker):
    _mock_embed(mocker)
    upsert = mocker.patch.object(pipeline, "upsert_papers")
    papers = [_paper(f"210{i}.0000{i}", date(2024, 1, i + 1)) for i in range(3)]

    total, earliest, latest = pipeline._consume(iter(papers), batch_size=2, label="test")

    assert total == 3
    assert [len(call.args[0]) for call in upsert.call_args_list] == [2, 1]  # one full batch, one flushed remainder


def test_consume_tracks_earliest_and_latest_published_date(mocker):
    _mock_embed(mocker)
    mocker.patch.object(pipeline, "upsert_papers")
    papers = [_paper(f"210{i}.0000{i}", date(2024, 1, i + 1)) for i in (2, 0, 1)]  # out of order

    _, earliest, latest = pipeline._consume(iter(papers), batch_size=10, label="test")

    assert (earliest, latest) == (date(2024, 1, 1), date(2024, 1, 3))


def test_consume_date_filter_excludes_from_tracking_but_not_from_ingestion(mocker):
    """PubMed's daily fetch uses this so a future PubDate can't push the checkpoint
    forward while the paper itself is still upserted."""
    _mock_embed(mocker)
    upsert = mocker.patch.object(pipeline, "upsert_papers")
    papers = [_paper("2101.00001", date(2024, 1, 1)), _paper("2101.00002", date(2099, 1, 1))]

    total, earliest, latest = pipeline._consume(
        iter(papers), batch_size=10, label="test", date_filter=lambda d: d.year < 2030
    )

    assert total == 2
    assert (earliest, latest) == (date(2024, 1, 1), date(2024, 1, 1))
    assert len(upsert.call_args.args[0]) == 2


def test_consume_ignores_papers_with_no_published_date(mocker):
    _mock_embed(mocker)
    mocker.patch.object(pipeline, "upsert_papers")

    _, earliest, latest = pipeline._consume(iter([_paper("2101.00001", None)]), batch_size=10, label="test")

    assert (earliest, latest) == (None, None)


def test_consume_empty_iterator_upserts_nothing(mocker):
    upsert = mocker.patch.object(pipeline, "upsert_papers")

    total, earliest, latest = pipeline._consume(iter([]), batch_size=10, label="test")

    assert (total, earliest, latest) == (0, None, None)
    upsert.assert_not_called()


def test_consume_survives_an_embedding_outage(mocker):
    """_embed_and_upsert degrades to upserting without embeddings rather than losing
    the batch."""
    mocker.patch.object(pipeline, "embed_texts", side_effect=httpx.TransportError("down"))
    upsert = mocker.patch.object(pipeline, "upsert_papers")

    total, _, _ = pipeline._consume(iter([_paper("2101.00001", date(2024, 1, 1))]), batch_size=10, label="test")

    assert total == 1
    upsert.assert_called_once()
