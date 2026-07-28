"""GO annotation file (GAF 2.2) loader: Gene -> Pathway PARTICIPATES_IN edges for one
species.

This is the non-human counterpart to ingest/reactome.py's extract_participates_in.
Reactome's current release covers 16 species and no plants at all, so a plant corpus has
no Reactome-derived pathway edges available -- GO's own per-species annotation files are
the substitute. See docs/instructions.md.
"""

import gzip
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from spokebio.models import ParticipatesIn

GAF_BASE_URL = "http://current.geneontology.org/annotations/gaf"
DEFAULT_GAF_DIR = "data/gaf"
# UniProt mnemonic, not an NCBI taxon id -- that's how GO names these files.
DEFAULT_SPECIES_CODE = "ORYSJ"  # Oryza sativa Japonica Group (taxon 39947)

_NUM_COLUMNS = 17
# Only biological_process terms exist as Pathway nodes (ingest/go.py loads that branch
# alone), so molecular_function and cellular_component rows have nothing to point at.
_BIOLOGICAL_PROCESS = "P"

# 0-based GAF 2.2 column positions.
_COL_SYMBOL = 2
_COL_QUALIFIER = 3
_COL_GO_ID = 4
_COL_EVIDENCE = 6
_COL_ASPECT = 8
_COL_SYNONYMS = 10

# Evidence-code trust ranking (lower = more trusted), applying docs/plant_schema.md's
# tiered-trust rule to GO's full code set. Broader than reactome.py's TAS/IEA-only
# ranking because a GAF carries the whole vocabulary. Unranked codes sort last (99),
# not dropped.
_EVIDENCE_RANK = {
    # Experimental.
    "EXP": 0, "IDA": 0, "IPI": 0, "IMP": 0, "IGI": 0, "IEP": 0,
    # High-throughput experimental.
    "HTP": 1, "HDA": 1, "HMP": 1, "HGI": 1, "HEP": 1,
    "TAS": 2,  # traceable author statement
    "IC": 3,  # inferred by curator
    # Sequence/structural similarity.
    "ISS": 4, "ISO": 4, "ISA": 4, "ISM": 4, "IGC": 4, "RCA": 4,
    # Phylogenetically inferred.
    "IBA": 5, "IBD": 5, "IKR": 5, "IRD": 5,
    "NAS": 6,  # non-traceable author statement
    "IEA": 7,  # inferred from electronic annotation (uncurated)
}


class GafExtraction(NamedTuple):
    """Extracted edges plus the counts of what got dropped on the way.

    Unlike reactome.py, the drop rate here can't be stated once in a docstring: it
    depends on how well the species' gene_info file covers the identifiers its GAF
    happens to use, so it has to be reported per run.
    """

    edges: list[ParticipatesIn]
    rows_considered: int
    dropped_negated: int
    dropped_unresolved: int
    dropped_duplicate: int


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
def ensure_gaf_file(
    species_code: str = DEFAULT_SPECIES_CODE,
    dir_path: str | Path = DEFAULT_GAF_DIR,
    source: str = "uniprot",
    force: bool = False,
) -> str:
    """Download one species' GAF (e.g. "ORYSJ-uniprot.gaf.gz") if not already cached."""
    filename = f"{species_code}-{source}.gaf.gz"
    path = Path(dir_path) / filename
    if path.exists() and not force:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{GAF_BASE_URL}/{filename}"
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with path.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(path)


def iter_gaf_rows(path: str | Path) -> Iterator[list[str]]:
    """Stream-parse a GAF: tab-delimited, `!`-prefixed comment/header lines, 17 columns."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("!"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != _NUM_COLUMNS:
                continue
            yield fields


def _gene_candidates(row: list[str]) -> Iterator[str]:
    """Identifiers a GAF row offers for its gene, best first: column 3's symbol (the
    RAP-DB locus id for rice, e.g. "Os01g0104100"), then column 11's synonyms."""
    symbol = row[_COL_SYMBOL].strip()
    if symbol:
        yield symbol
    for token in row[_COL_SYNONYMS].split("|"):
        token = token.strip()
        if token:
            yield token


def extract_participates_in(path: str | Path, crosswalk: dict[str, str]) -> GafExtraction:
    """Parse a GAF into Gene -> Pathway edges, resolving each gene to an existing
    ``ncbigene:``-namespaced Gene.gene_id via ``crosswalk`` (see gene_crosswalk.py).

    Keeps biological_process rows only, drops NOT-qualified rows (negative annotations --
    including them would assert the opposite of what the source says), and dedupes
    (gene, pathway) pairs by keeping the higher-trust evidence code.
    """
    best: dict[tuple[str, str], ParticipatesIn] = {}
    rows_considered = dropped_negated = dropped_unresolved = dropped_duplicate = 0

    for row in iter_gaf_rows(path):
        if row[_COL_ASPECT] != _BIOLOGICAL_PROCESS:
            continue
        rows_considered += 1

        # GAF qualifiers are pipe-separated; a bare "NOT" component negates the row.
        if "NOT" in row[_COL_QUALIFIER].split("|"):
            dropped_negated += 1
            continue

        gene_id = next((crosswalk[c] for c in _gene_candidates(row) if c in crosswalk), None)
        if gene_id is None:
            dropped_unresolved += 1
            continue

        evidence_code = row[_COL_EVIDENCE]
        key = (gene_id, row[_COL_GO_ID])
        existing = best.get(key)
        if existing is not None:
            dropped_duplicate += 1
            if _EVIDENCE_RANK.get(evidence_code, 99) >= _EVIDENCE_RANK.get(existing.evidence_code, 99):
                continue
        best[key] = ParticipatesIn(
            gene_id=gene_id, pathway_id=row[_COL_GO_ID], evidence_code=evidence_code
        )

    return GafExtraction(
        edges=list(best.values()),
        rows_considered=rows_considered,
        dropped_negated=dropped_negated,
        dropped_unresolved=dropped_unresolved,
        dropped_duplicate=dropped_duplicate,
    )
