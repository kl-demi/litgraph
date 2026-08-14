from collections.abc import Iterable, Iterator
from pathlib import Path

from spokebio.ingest._download import ensure_cached_file
from spokebio.models import DiseaseIsA, DiseaseXref

DOID_OBO_URL = "https://purl.obolibrary.org/obo/doid.obo"
DEFAULT_DOID_PATH = "data/doid.obo"

_MESH_XREF_PREFIX = "MESH:"


def ensure_doid_file(path: str | Path = DEFAULT_DOID_PATH, force: bool = False) -> str:
    """Download doid.obo if it isn't already cached locally. Free, one-time 7MB download,
    refreshed only when ``force=True``.
    """
    return ensure_cached_file(DOID_OBO_URL, Path(path), force)


def iter_term_stanzas(path: str | Path) -> Iterator[dict]:
    """Stream-parse doid.obo's ``[Term]`` stanzas. Different from go.py's parser because
    DO needs the ``xref`` and ``is_a`` fields that GO doesn't collect.
    """
    current: dict | None = None
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if line == "[Term]":
                if current is not None:
                    yield current
                current = {"id": None, "name": None, "is_obsolete": False, "xref": [], "is_a": []}
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
            elif key == "is_obsolete":
                current["is_obsolete"] = value == "true"
            elif key == "xref":
                current["xref"].append(value)
            elif key == "is_a":
                # "DOID:4 ! disease" -- drop the trailing comment OBO appends.
                current["is_a"].append(value.partition(" !")[0].strip())
    if current is not None:
        yield current


def _live_terms(stanzas: Iterable[dict]) -> dict[str, dict]:
    """DOID -> stanza, for non-obsolete terms only."""
    return {
        term["id"]: term
        for term in stanzas
        if term["id"] and term["id"].startswith("DOID:") and not term["is_obsolete"]
    }


def _mesh_ids(term: dict) -> list[str]:
    """The MeSH descriptor ids a DO term cross-references, namespaced to match
    Disease.disease_id."""
    return [f"mesh:{x[len(_MESH_XREF_PREFIX):]}" for x in term["xref"] if x.startswith(_MESH_XREF_PREFIX)]


def extract_disease_xrefs(stanzas: Iterable[dict]) -> Iterator[DiseaseXref]:
    """Yield one cross-reference from DOID to MeSH ID for a disease node.

    Disease nodes get Paper-[MENTIONS]->Disease relationships from Pubtator3, which
    uses MeSH IDs. They get descriptions and Disease-[IS_A]->Disease relationships
    from Disease Ontology, which uses DOIDs.
    
    Only ~30% of DO's terms carry a MeSH xref (as of the 2026-07-31 DO release), so 
    Disease is MeSH-keyed and has DOID as an optional property.

    Sometimes a MeSH id can map to several DOIDs, eg. "Inflammation" in MeSH gets 
    mapped to multiple specific diseases in DO. In such case, the lexicographically 
    smallest gets chosen.
    """
    live = _live_terms(stanzas)
    best: dict[str, tuple[str, str]] = {}
    for doid, term in live.items():
        if not term["name"]:
            continue
        for disease_id in _mesh_ids(term):
            existing = best.get(disease_id)
            if existing is None or doid < existing[0]:
                best[disease_id] = (doid, term["name"])
    for disease_id, (doid, name) in best.items():
        yield DiseaseXref(disease_id=disease_id, doid=doid, name=name)


def extract_is_a_edges(stanzas: Iterable[dict]) -> Iterator[DiseaseIsA]:
    """Get hierarchy edges for disease nodes: whether disease A is a subtype of disease B.
    
    IS_A relationships come from Disease Ontology (DO), which get mapped onto MeSH-keyed 
    Disease nodes. On DO terms that have no MeSH xref, the edge is drawn to its nearest
    MeSH-mapped ancestors.
    """
    live = _live_terms(stanzas)
    parents = {doid: [p for p in term["is_a"] if p in live] for doid, term in live.items()}
    mesh_of = {doid: _mesh_ids(term) for doid, term in live.items() if _mesh_ids(term)}

    seen: set[tuple[str, str]] = set()
    for doid in mesh_of:
        for ancestor in _nearest_mapped_ancestors(doid, parents, mesh_of):
            for child_id in mesh_of[doid]:
                for parent_id in mesh_of[ancestor]:
                    # Two DO terms can share a MeSH id; that is not a self-edge to write.
                    if child_id == parent_id or (child_id, parent_id) in seen:
                        continue
                    seen.add((child_id, parent_id))
                    yield DiseaseIsA(child_id=child_id, parent_id=parent_id)


def _nearest_mapped_ancestors(
    doid: str, parents: dict[str, list[str]], mesh_of: dict[str, list[str]]
) -> set[str]:
    """Find the closest MeSH-mapped ancestors of `doid`."""
    found: set[str] = set()
    visited: set[str] = set()
    stack = list(parents.get(doid, []))
    while stack:
        candidate = stack.pop()
        if candidate in visited:
            continue
        visited.add(candidate)
        if candidate in mesh_of:
            found.add(candidate)
        else:
            stack.extend(parents.get(candidate, []))
    return found
