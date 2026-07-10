import json
from datetime import datetime
from pathlib import Path

from litgraph.config import get_settings


def log_run(job: str, started_at: datetime, finished_at: datetime, total_papers: int, **extra) -> None:
    """Append a JSON-lines record for one ingestion run (backload/fetch-daily/enrich/...)."""
    path = Path(get_settings().run_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "job": job,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "total_papers": total_papers,
        **extra,
    }
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_runs() -> list[dict]:
    path = Path(get_settings().run_log_path)
    if not path.exists():
        return []
    runs = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    return runs
