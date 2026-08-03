"""Trait Ontology (TO) loader: the trait vocabulary as Trait nodes.

TO is Planteome's ontology of measurable plant traits -- the named dimension being
measured ("drought tolerance"), as distinct from an observed value. It is the trait-side
counterpart to ingest/go.py's Pathway nodes, and the target of Oryzabase's gene-trait
annotations (see ingest/oryzabase.py).

Reuses go.py's OBO stanza parser rather than duplicating it: both files are OBO 1.2 and
only the filtering rule differs.
"""

from collections.abc import Iterator
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from spokebio.models import Trait

TO_OBO_URL = "https://purl.obolibrary.org/obo/to.obo"
DEFAULT_TO_OBO_PATH = "data/to.obo"

# to.obo carries imported terms from other ontologies alongside its own -- as of the
# 2026-01-14 release: 1,697 TO terms but also 1,659 PO, 892 CHEBI, 220 GO, 134 PATO, 61
# NCBITaxon and a handful of BFO/UBERON/OBI. Loading every [Term] stanza would mint GO
# ids as Trait nodes (colliding conceptually with the Pathway nodes already keyed on
# them) and CHEBI ids as Trait nodes (a second namespace for something Compound already
# models) -- exactly the duplicate/disconnected-node failure docs/spoke_schema.md's
# Design Principle 5 warns about. So filter on the id prefix, not just on obsolescence.
_TO_ID_PREFIX = "TO:"


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
def ensure_to_obo_file(path: str | Path = DEFAULT_TO_OBO_PATH, force: bool = False) -> str:
    """Download to.obo if it isn't already cached locally. Free, no license/API key
    needed -- a ~1MB download, refreshed only when ``force=True``."""
    p = Path(path)
    if p.exists() and not force:
        return str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", TO_OBO_URL, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with p.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(p)


def extract_traits(stanzas: Iterator[dict]) -> Iterator[Trait]:
    """Filter OBO term stanzas down to non-obsolete Trait Ontology terms.

    Unlike go.py's extract_pathways this can't filter on ``namespace``: to.obo declares
    a single ``default-namespace`` in its header and emits no per-term namespace line,
    so every stanza's namespace parses as None. The id prefix is the discriminator.
    """
    for term in stanzas:
        if term["is_obsolete"]:
            continue
        term_id = term["id"]
        if not term_id or not term_id.startswith(_TO_ID_PREFIX) or not term["name"]:
            continue
        yield Trait(trait_id=term_id, name=term["name"], source_db="TO")
