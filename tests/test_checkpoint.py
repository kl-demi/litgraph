from datetime import UTC, datetime

from litgraph.ingest import checkpoint


def test_get_checkpoint_returns_none_when_job_has_never_run(mocker):
    mocker.patch.object(checkpoint, "run_read", return_value=[])
    assert checkpoint.get_checkpoint("some_job") is None


def test_get_checkpoint_returns_none_when_field_is_null(mocker):
    mocker.patch.object(checkpoint, "run_read", return_value=[{"last_seen_date": None}])
    assert checkpoint.get_checkpoint("some_job") is None


def test_get_checkpoint_parses_an_iso_string(mocker):
    mocker.patch.object(checkpoint, "run_read", return_value=[{"last_seen_date": "2024-01-01T00:00:00"}])
    assert checkpoint.get_checkpoint("some_job") == datetime(2024, 1, 1)


def test_get_checkpoint_unwraps_a_native_temporal_value(mocker):
    """ArcadeDB's Bolt driver returns its own DateTime type, not a Python datetime."""

    class FakeBoltDateTime:
        def to_native(self):
            return datetime(2024, 1, 1)

    mocker.patch.object(checkpoint, "run_read", return_value=[{"last_seen_date": FakeBoltDateTime()}])
    assert checkpoint.get_checkpoint("some_job") == datetime(2024, 1, 1)


def test_get_checkpoint_passes_the_job_through(mocker):
    run_read = mocker.patch.object(checkpoint, "run_read", return_value=[])
    checkpoint.get_checkpoint("arxiv_daily")
    assert run_read.call_args.kwargs["job"] == "arxiv_daily"


def test_set_checkpoint_writes_job_and_isoformat_date(mocker):
    write = mocker.patch.object(checkpoint, "run_write")
    checkpoint.set_checkpoint(datetime(2024, 1, 1, tzinfo=UTC), "some_job")
    assert write.call_args.kwargs["job"] == "some_job"
    assert write.call_args.kwargs["last_seen_date"] == "2024-01-01T00:00:00+00:00"


def test_set_checkpoint_stamps_last_run_at(mocker):
    write = mocker.patch.object(checkpoint, "run_write")
    checkpoint.set_checkpoint(datetime(2024, 1, 1, tzinfo=UTC), "some_job")
    assert "last_run_at" in write.call_args.kwargs
