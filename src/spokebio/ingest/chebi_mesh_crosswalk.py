import csv
import gzip
import re
from collections.abc import Iterable
from pathlib import Path

from spokebio.ingest._download import ensure_cached_file

# All three confirmed live (2026-07-27), free, no license/API key needed.
CHEBI_BASE_URL = "https://ftp.ebi.ac.uk/pub/databases/chebi/flat_files"
MESH_BASE_URL = "https://nlmpubs.nlm.nih.gov/projects/mesh"
BIOMAPPINGS_URL = (
    "https://raw.githubusercontent.com/biopragmatics/biomappings/main/src/biomappings/resources/positive.sssom.tsv"
)

DEFAULT_CHEBI_DIR = "data/chebi"
DEFAULT_MESH_DIR = "data/mesh"
DEFAULT_BIOMAPPINGS_DIR = "data/biomappings"
# MeSH publishes no stable "current" alias (confirmed live) -- unlike go-basic.obo or
# Reactome's download/current/, this needs bumping to a newer year occasionally.
DEFAULT_MESH_YEAR = 2025

# Larger files than go.py/reactome.py's, hence the longer timeout than ensure_cached_file's default.
_DOWNLOAD_TIMEOUT = 120.0

_CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")


def ensure_chebi_file(filename: str, dir_path: str | Path = DEFAULT_CHEBI_DIR, force: bool = False) -> str:
    """Download one of ChEBI's flat files (e.g. "compounds.tsv.gz",
    "database_accession.tsv.gz") if not already cached locally."""
    return ensure_cached_file(f"{CHEBI_BASE_URL}/{filename}", Path(dir_path) / filename, force, _DOWNLOAD_TIMEOUT)


def ensure_mesh_file(
    filename: str, year: int = DEFAULT_MESH_YEAR, dir_path: str | Path = DEFAULT_MESH_DIR, force: bool = False
) -> str:
    """Download one of MeSH's per-year ASCII files (e.g. "d2025.bin", "c2025.bin").
    ``year`` will need bumping occasionally -- see DEFAULT_MESH_YEAR's note."""
    path = Path(dir_path) / filename
    return ensure_cached_file(f"{MESH_BASE_URL}/{year}/asciimesh/{filename}", path, force, _DOWNLOAD_TIMEOUT)


def ensure_biomappings_file(dir_path: str | Path = DEFAULT_BIOMAPPINGS_DIR, force: bool = False) -> str:
    """Download Biomappings' curated "positive" mappings (expert-reviewed exact
    matches across many ontology pairs, including ChEBI<->MeSH)."""
    path = Path(dir_path) / "positive.sssom.tsv"
    return ensure_cached_file(BIOMAPPINGS_URL, path, force, _DOWNLOAD_TIMEOUT)


def _parse_chebi_id_to_accession(compounds_path: str | Path) -> dict[str, str]:
    """ChEBI's internal numeric id -> its public "CHEBI:<n>" accession, from
    compounds.tsv.gz."""
    mapping: dict[str, str] = {}
    with gzip.open(compounds_path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        for fields in reader:
            if len(fields) != len(header):
                continue
            accession = fields[idx["chebi_accession"]]
            if accession:
                mapping[fields[idx["id"]]] = accession
    return mapping


def _parse_chebi_accession_to_cas(
    database_accession_path: str | Path, id_to_accession: dict[str, str]
) -> dict[str, set[str]]:
    """ChEBI accession -> its CAS Registry Number(s), from database_accession.tsv.gz's
    ``type="CAS"`` rows."""
    mapping: dict[str, set[str]] = {}
    with gzip.open(database_accession_path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        for fields in reader:
            if len(fields) != len(header) or fields[idx["type"]] != "CAS":
                continue
            accession = id_to_accession.get(fields[idx["compound_id"]])
            if accession:
                mapping.setdefault(accession, set()).add(fields[idx["accession_number"]])
    return mapping


def _parse_mesh_cas_to_ui(mesh_bin_paths: Iterable[str | Path]) -> dict[str, set[str]]:
    """CAS Registry Number -> MeSH UI(s), from MeSH's descriptor (d*.bin) and
    supplementary concept (c*.bin) files. NB: a record's ``UI`` field comes LAST, after
    its ``RR`` (registry number) fields -- must buffer RR values per-record and only
    associate them with UI once reached (confirmed live: getting this order backwards
    silently returns zero matches instead of erroring).
    """
    mapping: dict[str, set[str]] = {}
    for path in mesh_bin_paths:
        pending_cas: set[str] = set()
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line == "*NEWRECORD":
                    pending_cas = set()
                    continue
                if line.startswith("RR = "):
                    value = line[5:].split(" ")[0]  # strip trailing "(name)" annotation
                    if _CAS_PATTERN.match(value):
                        pending_cas.add(value)
                elif line.startswith("UI = ") and pending_cas:
                    ui = line[5:]
                    for cas in pending_cas:
                        mapping.setdefault(cas, set()).add(ui)
    return mapping


def _parse_biomappings_chebi_to_mesh(path: str | Path) -> dict[str, str]:
    """ChEBI accession -> MeSH UI, from Biomappings' expert-curated exact-match file
    (SSSOM format, with a YAML frontmatter block before the real TSV header). Drops the
    rare (~0.1%, see build_crosswalk's docstring) case where a ChEBI id maps to more
    than one distinct MeSH id -- safer to skip than guess.
    """
    candidates: dict[str, set[str]] = {}
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    header_idx = next(i for i, line in enumerate(lines) if line.startswith("subject_id"))
    for line in lines[header_idx + 1 :]:
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 4:
            continue
        subject_id, object_id = fields[0], fields[3]
        if subject_id.startswith("chebi:") and object_id.startswith("mesh:"):
            chebi, mesh = subject_id, object_id
        elif subject_id.startswith("mesh:") and object_id.startswith("chebi:"):
            chebi, mesh = object_id, subject_id
        else:
            continue
        accession = chebi.replace("chebi:", "CHEBI:")
        candidates.setdefault(accession, set()).add(mesh.replace("mesh:", ""))
    return {accession: next(iter(mesh_ids)) for accession, mesh_ids in candidates.items() if len(mesh_ids) == 1}


def build_crosswalk(
    compounds_path: str | Path,
    database_accession_path: str | Path,
    mesh_bin_paths: Iterable[str | Path],
    biomappings_path: str | Path,
) -> dict[str, str]:
    """Build a ChEBI accession -> namespaced MeSH compound_id map (e.g. "CHEBI:16480"
    -> "mesh:D009569"), matching the existing Compound.compound_id key.

    Combines two independent methods -- a CAS Registry Number bridge (ChEBI's own CAS
    cross-references, joined against MeSH's RR fields) and Biomappings' expert-curated
    exact matches -- since neither alone covers most of what's needed (confirmed live
    against Reactome's human-relevant ChEBI ids: 27.9% and 15.1% respectively, 33.7%
    combined). Where both methods agree, that's a strong signal (confirmed live:
    296/297 overlapping cases agree). Where they disagree, Biomappings' curated answer
    wins -- expert review over a mechanical CAS-number match (exactly one such conflict
    was found in validation, out of 297 overlapping cases).
    """
    id_to_accession = _parse_chebi_id_to_accession(compounds_path)
    accession_to_cas = _parse_chebi_accession_to_cas(database_accession_path, id_to_accession)
    cas_to_mesh = _parse_mesh_cas_to_ui(mesh_bin_paths)
    biomappings_map = _parse_biomappings_chebi_to_mesh(biomappings_path)

    cas_bridge_map: dict[str, str] = {}
    for accession, cas_set in accession_to_cas.items():
        mesh_ids: set[str] = set()
        for cas in cas_set:
            mesh_ids |= cas_to_mesh.get(cas, set())
        if len(mesh_ids) == 1:  # drop the rare (~0.1%) ambiguous case rather than guess
            cas_bridge_map[accession] = next(iter(mesh_ids))

    crosswalk = dict(cas_bridge_map)
    crosswalk.update(biomappings_map)  # Biomappings wins on conflict
    return {accession: f"mesh:{mesh_id}" for accession, mesh_id in crosswalk.items()}
