from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from spokebio.ingest._download import ensure_cached_file
from spokebio.models import ParticipatesIn, Pathway, PathwayGoMapping, Produces

REACTOME_BASE_URL = "https://reactome.org/download/current"
DEFAULT_REACTOME_DIR = "data/reactome"
_HUMAN_SPECIES = "Homo sapiens"

# Evidence-code trust ranking (lower = more trusted), matching docs/spoke_schema.md's
# tiered-trust note: TAS (Traceable Author Statement, curator-traced to a specific
# publication) beats IEA (Inferred from Electronic Annotation, automated) when the same
# gene/pathway pair appears via both -- confirmed live, e.g. NCBI Gene 10000 x
# R-HSA-1257604 has one row of each. Unranked codes sort last (rank 99), not dropped.
_EVIDENCE_RANK = {"TAS": 0, "IEA": 1}


def ensure_reactome_file(filename: str, dir_path: str | Path = DEFAULT_REACTOME_DIR, force: bool = False) -> str:
    """Download one of Reactome's flat files (e.g. "ReactomePathways.txt",
    "NCBI2Reactome.txt") if not already cached locally."""
    return ensure_cached_file(f"{REACTOME_BASE_URL}/{filename}", Path(dir_path) / filename, force)


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


class ParticipatesInExtraction(NamedTuple):
    """Extracted edges plus counts of what got dropped on the way -- reported per run
    rather than left to a docstring's numbers, since a change in drop rate is the main
    signal that Reactome's file format or content shifted."""

    edges: list[ParticipatesIn]
    rows_considered: int
    dropped_duplicate: int


def extract_participates_in(path: str | Path) -> ParticipatesInExtraction:
    """Filter NCBI2Reactome.txt (gene_id, pathway_id, url, pathway_name, evidence_code,
    species) to Homo sapiens, deduping (gene, pathway) pairs by keeping the higher-trust
    evidence code when a pair appears via both (confirmed live: this happens for 4,076
    pairs in the real file). ``edges`` is a materialized list, not a generator -- dedup
    needs to see every row for a pair before it can decide which one wins.
    """
    best: dict[tuple[str, str], ParticipatesIn] = {}
    rows_considered = dropped_duplicate = 0
    for gene_id, pathway_id, _url, _pathway_name, evidence_code, species in _iter_tab_delimited_rows(path, 6):
        if species != _HUMAN_SPECIES:
            continue
        rows_considered += 1
        key = (gene_id, pathway_id)
        existing = best.get(key)
        if existing is not None:
            dropped_duplicate += 1
            if _EVIDENCE_RANK.get(evidence_code, 99) >= _EVIDENCE_RANK.get(existing.evidence_code, 99):
                continue
        best[key] = ParticipatesIn(gene_id=f"ncbigene:{gene_id}", pathway_id=pathway_id, evidence_code=evidence_code)
    return ParticipatesInExtraction(
        edges=list(best.values()), rows_considered=rows_considered, dropped_duplicate=dropped_duplicate
    )


class ProducesExtraction(NamedTuple):
    """Extracted edges plus counts of what got dropped on the way -- see
    ParticipatesInExtraction. ``dropped_unresolved`` is normally the large one: only
    ~33.7% of Reactome's human ChEBI ids resolve through the ChEBI<->MeSH crosswalk.
    """

    edges: list[Produces]
    rows_considered: int
    dropped_unresolved: int
    dropped_duplicate: int


def extract_produces(path: str | Path, crosswalk: dict[str, str]) -> ProducesExtraction:
    """Filter ChEBI2Reactome.txt (chebi_id, pathway_id, url, pathway_name,
    evidence_code, species) to Homo sapiens, resolving each bare ChEBI id to an
    existing Compound.compound_id via ``crosswalk`` (see chebi_mesh_crosswalk.py).
    Unresolved ids are dropped, since there's no other key to upsert a Compound
    against without inventing a second, chebi:-namespaced identity for compounds
    already keyed by MeSH id. Dedupes (compound, pathway) pairs by keeping the
    higher-trust evidence code when a pair appears via both (same issue as
    extract_participates_in: confirmed live, 1,056 duplicate pairs in the real file).
    """
    best: dict[tuple[str, str], Produces] = {}
    rows_considered = dropped_unresolved = dropped_duplicate = 0
    for chebi_id, pathway_id, _url, _pathway_name, evidence_code, species in _iter_tab_delimited_rows(path, 6):
        if species != _HUMAN_SPECIES:
            continue
        rows_considered += 1
        compound_id = crosswalk.get(f"CHEBI:{chebi_id}")
        if compound_id is None:
            dropped_unresolved += 1
            continue
        key = (compound_id, pathway_id)
        existing = best.get(key)
        if existing is not None:
            dropped_duplicate += 1
            if _EVIDENCE_RANK.get(evidence_code, 99) >= _EVIDENCE_RANK.get(existing.evidence_code, 99):
                continue
        best[key] = Produces(compound_id=compound_id, pathway_id=pathway_id, evidence_code=evidence_code)
    return ProducesExtraction(
        edges=list(best.values()),
        rows_considered=rows_considered,
        dropped_unresolved=dropped_unresolved,
        dropped_duplicate=dropped_duplicate,
    )


def extract_pathway_go_mappings(path: str | Path) -> list[PathwayGoMapping]:
    """Parse Pathways2GoTerms_human.txt (Identifier, Name, GO_Term) into Reactome
    Pathway -> GO Pathway correspondences.

    Unlike Reactome's other flat files, this one carries a header row. It's otherwise
    clean: one row per Reactome pathway id, no duplicate (Reactome id, GO id) pairs
    (confirmed live), so no dedup pass is needed. A GO id can be the target of several
    Reactome pathways (118 of 1,018 rows, confirmed live) -- that's expected fan-in, not
    a conflict to resolve.
    """
    mappings = []
    with open(path, encoding="utf-8") as f:
        next(f, None)  # header: Identifier, Name, GO_Term
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 3:
                continue
            reactome_pathway_id, _name, go_pathway_id = fields
            mappings.append(PathwayGoMapping(reactome_pathway_id=reactome_pathway_id, go_pathway_id=go_pathway_id))
    return mappings
