from collections.abc import Iterator
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from spokebio.models import Pathway

GO_OBO_URL = "https://purl.obolibrary.org/obo/go/go-basic.obo"
DEFAULT_OBO_PATH = "data/go-basic.obo"

# GO has three top-level branches; only biological_process maps to a Pathway node here,
# per docs/plant_schema.md's Pathway design ("GO ID for broader biological_process
# terms"). molecular_function/cellular_component describe a different kind of thing
# (what a gene product does / where it's located) and aren't in scope.
_BIOLOGICAL_PROCESS = "biological_process"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def ensure_obo_file(path: str | Path = DEFAULT_OBO_PATH, force: bool = False) -> str:
    """Download go-basic.obo if it isn't already cached locally. Free, no license/API
    key needed (unlike PlantCyc/MetaCyc) -- a ~30MB one-time bulk download, refreshed
    only when ``force=True`` (GO cuts a new release periodically; this doesn't
    auto-detect staleness).
    """
    p = Path(path)
    if p.exists() and not force:
        return str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", GO_OBO_URL, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with p.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(p)


def iter_term_stanzas(path: str | Path) -> Iterator[dict]:
    """Stream-parse an OBO file's ``[Term]`` stanzas (skipping ``[Typedef]`` and any
    other stanza type). Only extracts the handful of fields this module needs --
    ``id``, ``name``, ``namespace``, ``is_obsolete`` -- this is not a general-purpose
    OBO parser.
    """
    current: dict | None = None
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if line == "[Term]":
                if current is not None:
                    yield current
                current = {"id": None, "name": None, "namespace": None, "is_obsolete": False}
                continue
            if line.startswith("[") and line.endswith("]"):
                if current is not None:
                    yield current
                current = None
                continue
            if current is None or ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if key == "id":
                current["id"] = value
            elif key == "name":
                current["name"] = value
            elif key == "namespace":
                current["namespace"] = value
            elif key == "is_obsolete":
                current["is_obsolete"] = value == "true"
    if current is not None:
        yield current


def extract_pathways(stanzas: Iterator[dict]) -> Iterator[Pathway]:
    """Filter GO term stanzas down to non-obsolete biological_process terms -- the
    species-agnostic half of Pathway ingestion (see docs/plant_schema.md's Pathway
    scope note: GO for cross-species process-level claims, PlantCyc/MetaCyc for
    species-specific pathways, added separately once its license/PGDB files are in
    hand).
    """
    for term in stanzas:
        if term["is_obsolete"] or term["namespace"] != _BIOLOGICAL_PROCESS:
            continue
        if not term["id"] or not term["name"]:
            continue
        yield Pathway(pathway_id=term["id"], name=term["name"], source_db="GO")
