import time
from collections.abc import Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from litgraph.db.neo4j_client import chunked
from plantbio.models import EntityMention

# Confirmed against the live API (2026-07-21): 100 pmids in one request succeeds,
# 101 fails with HTTP 400 {"The field \"pmids\" can not be longer than 100"} -- this
# is a hard per-request ceiling PubTator3 enforces, not a tunable default.
EXPORT_BATCH_SIZE = 100

_VERTEX_TYPE_BY_ANNOTATION_TYPE = {"Gene": "Gene", "Chemical": "Compound", "Species": "Organism"}
# Disease (and everything else PubTator3 tags -- Mutation, CellLine, ...) is dropped
# outright, not just filtered downstream: docs/plant_schema.md's live test against a
# real plant paper found PubTator's disease tagging produces confident-looking false
# positives on ordinary plant-science words (e.g. "insect" mistagged as the disease
# "Entomophobia", which then fed a bogus extracted relation). No plant-biology use for
# it here.

# Organism's key is the bare NCBI Taxonomy id (a single global namespace already, see
# docs/plant_schema.md). Gene/Compound get a source prefix -- note PubTator3 normalizes
# chemicals to MeSH ids today (confirmed live, e.g. "MESH:D000241"), not ChEBI, despite
# plant_schema.md's Compound node originally being designed around a chebi_id key: a
# field literally named chebi_id holding a MeSH id would misrepresent the data, so
# graph/upsert of this module names it compound_id instead pending a real ChEBI/PubChem
# crosswalk.
_DB_PREFIXES = {"ncbi_gene": "ncbigene", "ncbi_mesh": "mesh", "ncbi_taxonomy": None}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _entity_name(annotation_type: str, infons: dict, text: str) -> str:
    # For Species, infons["name"] is just the taxon id again (e.g. "9606") -- the
    # mention text ("human", "Arabidopsis") is the only human-readable name PubTator
    # gives us. For Gene/Chemical, infons["name"] is the canonical symbol/name (e.g.
    # "AGO2", "Adenosine"), more useful than whatever synonym/abbreviation was actually
    # written in the source text.
    if annotation_type == "Species":
        return text or infons.get("name") or ""
    return infons.get("name") or text or ""


def extract_mentions(annotations: list[dict]) -> list[EntityMention]:
    """Filter a PubTator3 document's raw annotations down to normalized Gene/Chemical/
    Species mentions, deduped within the document. Drops anything unnormalized
    (``valid: false`` -- no stable key to upsert against) and anything outside the
    three kept types.
    """
    seen: set[tuple[str, str]] = set()
    mentions: list[EntityMention] = []
    for ann in annotations:
        infons = ann.get("infons") or {}
        annotation_type = infons.get("type")
        vertex_type = _VERTEX_TYPE_BY_ANNOTATION_TYPE.get(annotation_type)
        if vertex_type is None or not infons.get("valid"):
            continue

        raw_id = infons.get("normalized_id")
        if raw_id is None:
            raw_id = infons.get("identifier")
        if raw_id is None:
            continue
        identifier = str(raw_id)

        database = infons.get("database")
        prefix = _DB_PREFIXES.get(database, database)
        entity_id = f"{prefix}:{identifier}" if prefix else identifier

        key = (vertex_type, entity_id)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(
            EntityMention(
                vertex_type=vertex_type,
                entity_id=entity_id,
                name=_entity_name(annotation_type, infons, ann.get("text", "")),
            )
        )
    return mentions


class PubTatorClient:
    """Thin client for PubTator3's BioC-JSON export endpoint. Modeled on
    SemanticScholarClient (litgraph/ingest/semantic_scholar.py): synchronous httpx, a
    sleep-based throttle, tenacity retry on 429/5xx/transport errors. PubTator3 has no
    documented rate limit or API key, so ``requests_per_second`` defaults
    conservatively (matching NCBI E-utilities' no-API-key ceiling) -- raise it only if
    you've confirmed PubTator3 tolerates more.
    """

    BASE_URL = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"

    def __init__(self, requests_per_second: float = 3.0) -> None:
        self._min_interval = 1.0 / requests_per_second
        self._last_request_at: float | None = None
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PubTatorClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _export_batch(self, pmids: list[str]) -> dict:
        self._throttle()
        response = self._client.get("/publications/export/biocjson", params={"pmids": ",".join(pmids)})
        response.raise_for_status()
        return response.json()

    def fetch_mentions(self, pmids: list[str]) -> Iterator[tuple[str, list[EntityMention]]]:
        """Yield ``(pmid, mentions)`` for each requested pmid PubTator3 has annotations
        for, batching requests at its hard ceiling of 100 PMIDs per call. PMIDs it
        doesn't recognize are silently absent from the response (confirmed live, not an
        error) -- callers should expect fewer results than requested.
        """
        for batch in chunked(pmids, EXPORT_BATCH_SIZE):
            data = self._export_batch(batch)
            for doc in data.get("PubTator3", []):
                pmid = str(doc.get("pmid"))
                annotations = [ann for passage in doc.get("passages", []) for ann in passage.get("annotations", [])]
                yield pmid, extract_mentions(annotations)
