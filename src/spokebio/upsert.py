"""Biology-side graph writes: nodes, annotation edges, and gene-name maintenance.

Exports:
    upsert_pathways: ontology term nodes (update on match).
    upsert_participates_in / upsert_produces: annotation edges (bootstrap the entity
        endpoint, require the ontology one).
    upsert_mentions: Paper->entity MENTIONS edges plus the entity nodes themselves.
    mark_papers_checked: per-extractor ExtractionChecked bookkeeping nodes.
    backfill_gene_names: Gene display-name maintenance.

Usage: extract.py/pipeline.py call one upsert per extractor batch; each function maps its
model objects to rows and picks the create/update policy for `litgraph.graph.writer`.
"""

from datetime import datetime

from litgraph.db.neo4j_client import run_write
from litgraph.graph.writer import CreateMissing, upsert_edges, upsert_nodes
from spokebio.models import EntityMention, ParticipatesIn, Pathway, Produces

# The MENTIONS destination type per EntityMention.vertex_type. MENTIONS is registered
# Paper -> Gene, and the others are passed as a `dst` override.
_MENTION_TARGETS = ("Organism", "Gene", "Compound", "Disease")
_KEY_PROP = {
    "Organism": "taxon_id",
    "Gene": "gene_id",
    "Compound": "compound_id",
    "Disease": "disease_id",
}
_STAT_KEY = {
    "Organism": "new_organisms",
    "Gene": "new_genes",
    "Compound": "new_compounds",
    "Disease": "new_diseases",
}


def upsert_pathways(pathways: list[Pathway]) -> int:
    """Upsert Pathway nodes from GO's biological_process branch and Reactome.

    Returns:
        int: How many nodes were newly created.
    """
    rows = [{"pathway_id": p.pathway_id, "name": p.name, "source_db": p.source_db} for p in pathways]
    return upsert_nodes("Pathway", rows, update_existing=True)


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


def upsert_mentions(paper_mentions: dict[str, list[EntityMention]], source: str) -> dict[str, int]:
    """Upsert Gene/Compound/Organism/Disease nodes and MENTIONS edges for a batch of papers.

    Neither endpoint is bootstrapped: an entity is written as a node in the pass above, and
    a paper that isn't in the graph isn't one this run should invent.

    Conflict rule: when two extractors produce the same (paper, entity) edge, the first
    writer wins -- `source` is set on creation only and edge properties are never updated,
    so a re-run or second extractor can't take over the attribution.

    Args:
        paper_mentions: Paper.id -> the mentions found for it; an empty list means no edges.
        source: Extractor that produced the edges (`Extractor.name`), e.g. "pubtator3".

    Returns:
        dict[str, int]: Counts of newly created nodes and edges, plus genes named.
    """
    stats = {
        "new_organisms": 0,
        "new_genes": 0,
        "new_compounds": 0,
        "new_diseases": 0,
        "new_mention_edges": 0,
        "genes_named": 0,
    }
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
            rows = [
                {"src": paper_id, "dst": entity_id, "source": source}
                for paper_id, entity_id in edges[target]
            ]
            stats["new_mention_edges"] += upsert_edges(
                "MENTIONS", rows, create_missing=CreateMissing.NONE, update_existing=False, dst=target
            )

    return stats


_MARK_CHECKED = """
UNWIND $rows AS row
MERGE (c:ExtractionChecked {check_id: row.check_id})
ON CREATE SET c.extractor = $extractor, c.paper_id = row.paper_id, c.checked_at = $checked_at
"""


def mark_papers_checked(extractor: str, paper_ids: list[str], checked_at: datetime) -> None:
    """Record that `extractor` has been run against these papers, so a re-run skips them
    whether or not any mentions survived the filter. Keyed per extractor, so a second
    extractor still sees the paper as unchecked."""
    if not paper_ids:
        return
    rows = [{"check_id": f"{extractor}:{pid}", "paper_id": pid} for pid in paper_ids]
    run_write(_MARK_CHECKED, rows=rows, extractor=extractor, checked_at=checked_at.isoformat())


# Sets `name` only where it is currently null, so nothing already named is overwritten.
_BACKFILL_GENE_NAMES = """
UNWIND $genes AS g
MATCH (n:Gene {gene_id: g.gene_id})
WHERE n.name IS NULL
SET n.name = g.name
RETURN count(n) AS named
"""

def backfill_gene_names(names: dict[str, str]) -> int:
    """Give a readable symbol to Gene nodes that have none.

    Args:
        names: gene_id -> symbol.

    Returns:
        int: How many genes were named.
    """
    if not names:
        return 0
    params = [{"gene_id": k, "name": v} for k, v in names.items()]
    return run_write(_BACKFILL_GENE_NAMES, genes=params)[0]["named"]
