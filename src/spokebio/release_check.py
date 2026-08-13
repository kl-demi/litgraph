"""Check GO, Reactome, and Disease Ontology for a new release, re-ingesting
(force_download=True) only the sources that changed. A no-op most days.

GO's and DO's releases are read from the `data-version` line in their own OBO headers,
fetched via a small byte-range GET (3KB, not the full ~30MB/~7MB file; both servers
confirmed to honour Range). Reactome's release is read from its ContentService version
endpoint. All confirmed live by inspection -- GO/Reactome 2026-07-27, DO 2026-08-12 --
not from published API docs, so if any changes format this raises rather than silently
misreporting the version.
"""

import json
import re
from pathlib import Path

import httpx

from litgraph.config import get_settings
from spokebio.pipeline import run_disease_ontology_ingest, run_go_ingest, run_reactome_ingest

GO_OBO_URL = "https://purl.obolibrary.org/obo/go/go-basic.obo"
DOID_OBO_URL = "https://purl.obolibrary.org/obo/doid.obo"
REACTOME_VERSION_URL = "https://reactome.org/ContentService/data/database/version"

_SOURCES = ("go", "reactome", "disease_ontology")
_INGEST = {
    "go": lambda: run_go_ingest(force_download=True),
    "reactome": lambda: run_reactome_ingest(force_download=True),
    "disease_ontology": lambda: run_disease_ontology_ingest(force_download=True),
}


def default_state_path() -> Path:
    """One state file per database. A single shared file would report a source as
    "unchanged" for a database that was never actually ingested, because the version
    was recorded while checking a *different* database -- silently skipping
    ingestion rather than erroring."""
    return Path(f"data/pathway_release_state.{get_settings().arcadedb_database}.json")


def _obo_data_version(url: str, filename: str) -> str:
    resp = httpx.get(url, headers={"Range": "bytes=0-3000"}, follow_redirects=True, timeout=30.0)
    resp.raise_for_status()
    match = re.search(r"^data-version:\s*(\S+)", resp.text, re.MULTILINE)
    if not match:
        raise ValueError(f"data-version line not found in {filename} header -- format may have changed")
    return match.group(1)


def get_go_release() -> str:
    return _obo_data_version(GO_OBO_URL, "go-basic.obo")


def get_disease_ontology_release() -> str:
    return _obo_data_version(DOID_OBO_URL, "doid.obo")


def get_reactome_release() -> str:
    resp = httpx.get(REACTOME_VERSION_URL, timeout=30.0)
    resp.raise_for_status()
    return resp.text.strip()


# Lambdas, not bare function references: a bare reference here would freeze in the
# original function object at import time, so patching the module-level name later
# (e.g. in a test) would silently miss it. A lambda re-resolves the name on every call.
_GET_RELEASE = {
    "go": lambda: get_go_release(),
    "reactome": lambda: get_reactome_release(),
    "disease_ontology": lambda: get_disease_ontology_release(),
}


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def check_and_reingest(state_path: Path | None = None) -> dict[str, dict]:
    """Compare each source's current release against `state_path`'s last-seen one and
    re-ingest whatever changed.

    Returns one entry per source in `_SOURCES`:
        {"previous": str | None, "current": str, "changed": bool, "totals": dict | None}
    `totals` is the ingest function's own return value, present only when `changed`.
    """
    path = state_path or default_state_path()
    state = load_state(path)
    current = {source: _GET_RELEASE[source]() for source in _SOURCES}

    results = {}
    for source in _SOURCES:
        previous = state.get(source)
        changed = current[source] != previous
        results[source] = {
            "previous": previous,
            "current": current[source],
            "changed": changed,
            "totals": _INGEST[source]() if changed else None,
        }

    save_state(path, current)
    return results
