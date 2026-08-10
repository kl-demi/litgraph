"""IngestState checkpoint: one MERGE-backed cursor per named job.

Exports:
    get_checkpoint(job): last_seen_date for a job, or None if it has never run.
    set_checkpoint(date, job): record last_seen_date + last_run_at for a job.

Usage: callers choose the job name (e.g. "arxiv_daily",
"pubmed_backload_api:<mesh_terms>")
"""

from datetime import UTC, datetime

from litgraph.db.neo4j_client import run_read, run_write

_GET_CHECKPOINT = """
MATCH (s:IngestState {job: $job})
RETURN s.last_seen_date AS last_seen_date
"""

_SET_CHECKPOINT = """
MERGE (s:IngestState {job: $job})
SET s.last_seen_date = $last_seen_date, s.last_run_at = $last_run_at
"""


def get_checkpoint(job: str) -> datetime | None:
    rows = run_read(_GET_CHECKPOINT, job=job)
    if not rows or rows[0]["last_seen_date"] is None:
        return None
    value = rows[0]["last_seen_date"]
    if hasattr(value, "to_native"):
        return value.to_native()
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def set_checkpoint(last_seen_date: datetime, job: str) -> None:
    run_write(
        _SET_CHECKPOINT,
        job=job,
        last_seen_date=last_seen_date.isoformat(),
        last_run_at=datetime.now(UTC).isoformat(),
    )
