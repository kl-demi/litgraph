from collections.abc import Iterator
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from spokebio.models import ParticipatesIn, Pathway

# Confirmed live (2026-07-24): a plain public directory, HTTP 200, no license/account/
# API key needed -- unlike PlantCyc/MetaCyc.
REACTOME_BASE_URL = "https://reactome.org/download/current"
DEFAULT_REACTOME_DIR = "data/reactome"
_HUMAN_SPECIES = "Homo sapiens"

# Evidence-code trust ranking (lower = more trusted), matching docs/spoke_schema.md's
# tiered-trust note: TAS (Traceable Author Statement, curator-traced to a specific
# publication) beats IEA (Inferred from Electronic Annotation, automated) when the same
# gene/pathway pair appears via both -- confirmed live, e.g. NCBI Gene 10000 x
# R-HSA-1257604 has one row of each. Unranked codes sort last (rank 99), not dropped.
_EVIDENCE_RANK = {"TAS": 0, "IEA": 1}


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
def ensure_reactome_file(filename: str, dir_path: str | Path = DEFAULT_REACTOME_DIR, force: bool = False) -> str:
    """Download one of Reactome's flat files (e.g. "ReactomePathways.txt",
    "NCBI2Reactome.txt") if not already cached locally."""
    path = Path(dir_path) / filename
    if path.exists() and not force:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{REACTOME_BASE_URL}/{filename}"
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with path.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(path)


def _iter_tab_delimited_rows(path: str | Path, num_columns: int) -> Iterator[list[str]]:
    """Stream-parse one of Reactome's flat files: plain tab-delimited, no header row."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != num_columns:
                continue
            yield fields


def extract_human_pathways(path: str | Path) -> Iterator[Pathway]:
    """Filter ReactomePathways.txt (pathway_id, name, species) to Homo sapiens.
    Pathway ids are already bare and self-namespaced (e.g. "R-HSA-164843"), same as GO's
    "GO:..." -- no synthetic prefix needed (see docs/spoke_schema.md)."""
    for pathway_id, name, species in _iter_tab_delimited_rows(path, 3):
        if species != _HUMAN_SPECIES:
            continue
        yield Pathway(pathway_id=pathway_id, name=name, source_db="Reactome")


def extract_participates_in(path: str | Path) -> list[ParticipatesIn]:
    """Filter NCBI2Reactome.txt (gene_id, pathway_id, url, pathway_name, evidence_code,
    species) to Homo sapiens, deduping (gene, pathway) pairs by keeping the higher-trust
    evidence code when a pair appears via both (confirmed live: this happens for 4,076
    pairs in the real file). Returns a materialized list, not a generator -- dedup needs
    to see every row for a pair before it can decide which one wins.
    """
    best: dict[tuple[str, str], ParticipatesIn] = {}
    for gene_id, pathway_id, _url, _pathway_name, evidence_code, species in _iter_tab_delimited_rows(path, 6):
        if species != _HUMAN_SPECIES:
            continue
        key = (gene_id, pathway_id)
        existing = best.get(key)
        if existing is not None and _EVIDENCE_RANK.get(evidence_code, 99) >= _EVIDENCE_RANK.get(
            existing.evidence_code, 99
        ):
            continue
        best[key] = ParticipatesIn(gene_id=f"ncbigene:{gene_id}", pathway_id=pathway_id, evidence_code=evidence_code)
    return list(best.values())
