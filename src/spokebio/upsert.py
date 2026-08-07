"""Biology-side graph writes: nodes, annotation edges, and gene-name maintenance.

Exports:
    upsert_pathways / upsert_traits: ontology term nodes (update on match).
    upsert_participates_in / upsert_associated_with / upsert_produces: annotation edges
        (bootstrap the entity endpoint, require the ontology one).
    upsert_mentions: Paper->entity MENTIONS edges plus the entity nodes themselves.
    mark_papers_checked: PubtatorChecked bookkeeping nodes.
    backfill_gene_names / read_gene_names / upgrade_gene_names /
        backfill_gene_locus_ids / find_genes_by_locus_id: Gene display-name and locus-id
        maintenance.

Usage: pipeline.py calls one upsert per extractor batch; each function maps its model
objects to rows and picks the create/update policy for `litgraph.graph.writer`.
"""

from datetime import datetime

from litgraph.db.neo4j_client import run_read, run_write
from litgraph.graph.writer import CreateMissing, upsert_edges, upsert_nodes
from spokebio.models import AssociatedWith, EntityMention, ParticipatesIn, Pathway, Produces, Trait

# The MENTIONS destination type per EntityMention.vertex_type. MENTIONS is registered
# Paper -> Gene, and the other two are passed as a `dst` override.
_MENTION_TARGETS = ("Organism", "Gene", "Compound")
_KEY_PROP = {"Organism": "taxon_id", "Gene": "gene_id", "Compound": "compound_id"}
_STAT_KEY = {"Organism": "new_organisms", "Gene": "new_genes", "Compound": "new_compounds"}


def upsert_pathways(pathways: list[Pathway]) -> int:
    """Upsert Pathway nodes from GO's biological_process branch and Reactome.

    Returns:
        int: How many nodes were newly created.
    """
    rows = [{"pathway_id": p.pathway_id, "name": p.name, "source_db": p.source_db} for p in pathways]
    return upsert_nodes("Pathway", rows, update_existing=True)


def upsert_traits(traits: list[Trait]) -> int:
    """Upsert Trait nodes from the Trait Ontology.

    Returns:
        int: How many nodes were newly created.
    """
    rows = [{"trait_id": t.trait_id, "name": t.name, "source_db": t.source_db} for t in traits]
    return upsert_nodes("Trait", rows, update_existing=True)


def upsert_participates_in(edges: list[ParticipatesIn]) -> int:
    """Upsert Gene -> Pathway edges from Reactome or a GAF.

    Inserts a key-only Gene when absent, since most annotated genes have no node until
    literature names one. An absent Pathway drops the row instead: `run_go_ingest` must
    have run first, and a `Pathway` with no name or source_db would hide that it hadn't.

    Returns:
        int: How many edges were newly created.
    """
    rows = [{"src": e.gene_id, "dst": e.pathway_id, "evidence_code": e.evidence_code} for e in edges]
    return upsert_edges("PARTICIPATES_IN", rows, create_missing=CreateMissing.SRC, update_existing=True)


def upsert_associated_with(edges: list[AssociatedWith]) -> int:
    """Upsert Gene -> Trait edges from Oryzabase.

    Same endpoint policy as `upsert_participates_in`.

    Returns:
        int: How many edges were newly created.
    """
    rows = [{"src": e.gene_id, "dst": e.trait_id, "source_db": e.source_db} for e in edges]
    return upsert_edges("ASSOCIATED_WITH", rows, create_missing=CreateMissing.SRC, update_existing=True)


def upsert_produces(edges: list[Produces]) -> int:
    """Upsert Pathway -> Compound edges from Reactome via the ChEBI<->MeSH crosswalk.

    Inserts a key-only Compound when absent, safe because the crosswalk has already resolved
    it to the `mesh:` namespace -- an unresolved ChEBI id is dropped upstream, never keyed
    here as a second identity for the same compound.

    Returns:
        int: How many edges were newly created.
    """
    rows = [{"src": e.pathway_id, "dst": e.compound_id, "evidence_code": e.evidence_code} for e in edges]
    return upsert_edges("PRODUCES", rows, create_missing=CreateMissing.DST, update_existing=True)


def upsert_mentions(paper_mentions: dict[str, list[EntityMention]], source: str | None = None) -> dict[str, int]:
    """Upsert Gene/Compound/Organism nodes and MENTIONS edges for a batch of papers.

    Neither endpoint is bootstrapped: an entity is written as a node in the pass above, and
    a paper that isn't in the graph isn't one this run should invent.

    Args:
        paper_mentions: Paper.id -> the mentions found for it; an empty list means no edges.
        source: Extractor that produced the edges, e.g. "pubtator3". Set on creation only,
            so whichever extractor found a mention first keeps the attribution.

    Returns:
        dict[str, int]: Counts of newly created nodes and edges, plus genes named.
    """
    stats = {"new_organisms": 0, "new_genes": 0, "new_compounds": 0, "new_mention_edges": 0, "genes_named": 0}
    if not paper_mentions:
        return stats

    entities: dict[str, dict[str, EntityMention]] = {target: {} for target in _MENTION_TARGETS}
    edges: dict[str, set[tuple[str, str]]] = {target: set() for target in _MENTION_TARGETS}
    for paper_id, mentions in paper_mentions.items():
        for mention in mentions:
            entities[mention.vertex_type][mention.entity_id] = mention
            edges[mention.vertex_type].add((paper_id, mention.entity_id))

    for target in _MENTION_TARGETS:
        found = list(entities[target].values())
        if found:
            key_prop = _KEY_PROP[target]
            rows = [{key_prop: e.entity_id, "name": e.name} for e in found]
            stats[_STAT_KEY[target]] = upsert_nodes(target, rows, update_existing=False)
            if target == "Gene":
                stats["genes_named"] += backfill_gene_names({e.entity_id: e.name for e in found if e.name})

        if edges[target]:
            rows = [{"src": paper_id, "dst": entity_id} for paper_id, entity_id in edges[target]]
            if source:
                for row in rows:
                    row["source"] = source
            stats["new_mention_edges"] += upsert_edges(
                "MENTIONS", rows, create_missing=CreateMissing.NONE, update_existing=False, dst=target
            )

    return stats


_MARK_CHECKED = """
UNWIND $paper_ids AS pid
MERGE (c:PubtatorChecked {paper_id: pid})
ON CREATE SET c.checked_at = $checked_at
"""


def mark_papers_checked(paper_ids: list[str], checked_at: datetime) -> None:
    """Record that PubTator3 has been queried for these papers, so a re-run skips them
    whether or not any mentions survived the filter."""
    if not paper_ids:
        return
    run_write(_MARK_CHECKED, paper_ids=paper_ids, checked_at=checked_at.isoformat())


# Sets `name` only where it is currently null, so nothing already named is overwritten.
_BACKFILL_GENE_NAMES = """
UNWIND $genes AS g
MATCH (n:Gene {gene_id: g.gene_id})
WHERE n.name IS NULL
SET n.name = g.name
RETURN count(n) AS named
"""

_READ_GENE_NAMES = """
MATCH (n:Gene)
WHERE n.name IS NOT NULL
RETURN n.gene_id AS gene_id, n.name AS name
"""

# Unlike _BACKFILL_GENE_NAMES this SETs unconditionally, so callers must have confirmed the
# current name is a positional fallback. The guard lives in the caller because deciding
# "is this a locus id" is a regex judgement, and ArcadeDB's Cypher has no dependable regex.
_UPGRADE_GENE_NAMES = """
UNWIND $genes AS g
MATCH (n:Gene {gene_id: g.gene_id})
SET n.name = g.name
RETURN count(n) AS upgraded
"""


def backfill_gene_names(names: dict[str, str]) -> int:
    """Give a readable symbol to Gene nodes that have none.

    GAF- and Oryzabase-bootstrapped genes are created key-only, since those sources are
    keyed on locus ids and carry no symbol.

    Args:
        names: gene_id -> symbol.

    Returns:
        int: How many genes were named.
    """
    if not names:
        return 0
    params = [{"gene_id": k, "name": v} for k, v in names.items()]
    return run_write(_BACKFILL_GENE_NAMES, genes=params)[0]["named"]


def read_gene_names() -> dict[str, str]:
    """Every Gene node's current display name, for deciding which are safe to upgrade."""
    return {row["gene_id"]: row["name"] for row in run_read(_READ_GENE_NAMES)}


def upgrade_gene_names(names: dict[str, str]) -> int:
    """Replace a Gene's display name, for locus-id fallbacks a curated symbol supersedes.

    Args:
        names: gene_id -> symbol. Callers must have verified via `read_gene_names` that each
            current name is a bare locus id (`gene_crosswalk.is_locus_id`).

    Returns:
        int: How many names were replaced.
    """
    if not names:
        return 0
    params = [{"gene_id": k, "name": v} for k, v in names.items()]
    return run_write(_UPGRADE_GENE_NAMES, genes=params)[0]["upgraded"]


# Null-only, same additive-only discipline as _BACKFILL_GENE_NAMES: locus_id is a stable
# fact about a gene, so once set there is nothing to correct, and re-running must not
# thrash it if a later gene_info release reshuffles which locus id it lists first.
_BACKFILL_GENE_LOCUS_IDS = """
UNWIND $genes AS g
MATCH (n:Gene {gene_id: g.gene_id})
WHERE n.locus_id IS NULL
SET n.locus_id = g.locus_id
RETURN count(n) AS assigned
"""


def backfill_gene_locus_ids(loci: dict[str, str]) -> int:
    """Attach the community locus id (RAP-DB, else MSU/TIGR) to Gene nodes lacking one.

    Secondary lookup key, not an identity: ``gene_id`` stays canonical, so this never creates
    or re-keys a node. It exists so sources that cite a locus id -- which is most rice
    literature and every rice annotation file -- can find a gene directly instead of going
    through a crosswalk, and so `name` no longer has to double as an identifier. Returns the
    count assigned.
    """
    if not loci:
        return 0
    params = [{"gene_id": k, "locus_id": v} for k, v in loci.items()]
    return run_write(_BACKFILL_GENE_LOCUS_IDS, genes=params)[0]["assigned"]


_FIND_GENES_BY_LOCUS = """
UNWIND $locus_ids AS lid
MATCH (n:Gene {locus_id: lid})
RETURN lid AS locus_id, n.gene_id AS gene_id
"""


def find_genes_by_locus_id(locus_ids: list[str]) -> dict[str, list[str]]:
    """Resolve community locus ids to Gene.gene_id via the secondary index.

    Returns a list per locus id, not a single value: 103 of rice's locus ids legitimately map
    to more than one NCBI gene (see schema_ext._SECONDARY_KEYS), so collapsing to one would
    silently pick an arbitrary gene. Callers decide what to do with an ambiguous hit.
    """
    if not locus_ids:
        return {}
    resolved: dict[str, list[str]] = {}
    for row in run_read(_FIND_GENES_BY_LOCUS, locus_ids=locus_ids):
        resolved.setdefault(row["locus_id"], []).append(row["gene_id"])
    return resolved
