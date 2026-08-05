from datetime import datetime

from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_read, run_write
from spokebio.models import AssociatedWith, EntityMention, ParticipatesIn, Pathway, Produces, Trait

_KEY_PROP = {"Organism": "taxon_id", "Gene": "gene_id", "Compound": "compound_id"}
_STAT_KEY = {"Organism": "new_organisms", "Gene": "new_genes", "Compound": "new_compounds"}

# Same shape as graph/upsert.py's _UPSERT_STUBS_SQL / _UPSERT_CITATION_EDGES_SQL

# SELECT then INSERT if MISSING, one call per entity type per batch
def _upsert_entities_sql(vertex_type: str, key_prop: str) -> str:
    return f"""
BEGIN;
LET entities = :entities;
LET newCount = 0;
FOREACH ($e IN $entities) {{
  LET existing = SELECT FROM {vertex_type} WHERE {key_prop} = $e.entity_id;
  IF ($existing.size() = 0) {{
    INSERT INTO {vertex_type} SET {key_prop} = $e.entity_id, name = $e.name;
    LET newCount = $newCount + 1;
  }}
}}
COMMIT;
RETURN $newCount;
"""

# SELECTs both the Paper and the entity by natural key to get their @rids,
# checks for existing MENTIONS edge and CREATE EDGE if none exists.
#
# `source` stamps which extractor produced the edge (e.g. "pubtator3", "oryzabase-gazetteer")
# so the two can be told apart, measured against each other, and one of them reverted
# without touching the other. Omitted -> no property is set, which is exactly the old
# behaviour; the ~206K MENTIONS edges written before this existed carry no `source` and are
# all PubTator3's. An existing edge is never re-stamped, so whichever extractor found a
# mention first keeps the attribution.
def _upsert_mentions_sql(vertex_type: str, key_prop: str, source: str | None = None) -> str:
    set_source = f" SET source = '{source}'" if source else ""
    return f"""
BEGIN;
LET mentions = :mentions;
LET newCount = 0;
FOREACH ($m IN $mentions) {{
  LET paperRows = SELECT FROM Paper WHERE id = $m.paper_id;
  LET entityRows = SELECT FROM {vertex_type} WHERE {key_prop} = $m.entity_id;
  IF ($paperRows.size() > 0 AND $entityRows.size() > 0) {{
    LET paperRid = $paperRows[0].@rid;
    LET entityRid = $entityRows[0].@rid;
    LET existingEdges = SELECT FROM MENTIONS WHERE @out = $paperRid AND @in = $entityRid;
    IF ($existingEdges.size() = 0) {{
      CREATE EDGE MENTIONS FROM $paperRid TO $entityRid{set_source};
      LET newCount = $newCount + 1;
    }}
  }}
}}
COMMIT;
RETURN $newCount;
"""


_MARK_CHECKED = """
UNWIND $paper_ids AS pid
MERGE (c:PubtatorChecked {paper_id: pid})
ON CREATE SET c.checked_at = $checked_at
"""

# Plain Cypher/Bolt MERGE (unlike upsert_mentions above) -- Pathway nodes don't touch
# Paper at all in this pass, so there's no vector-index-bug risk to route around.
_UPSERT_PATHWAYS = """
UNWIND $pathways AS p
MERGE (pw:Pathway {pathway_id: p.pathway_id})
ON CREATE SET pw._is_new = true
WITH pw, p, coalesce(pw._is_new, false) AS is_new
REMOVE pw._is_new
SET pw.name = p.name, pw.source_db = p.source_db
RETURN count(CASE WHEN is_new THEN 1 END) AS new_pathways
"""


def upsert_mentions(paper_mentions: dict[str, list[EntityMention]], source: str | None = None) -> dict[str, int]:
    """Upsert Gene/Compound/Organism nodes and MENTIONS edges for a batch of papers.

    ``paper_mentions`` maps litgraph Paper.id -> the mentions found for it (an empty list
    means no edges). ``source`` optionally stamps new edges with the extractor that produced
    them -- see _upsert_mentions_sql. Returns counts of newly created nodes/edges.
    """
    settings = get_settings()
    if settings.graph_backend != "arcadedb":
        raise NotImplementedError("spokebio upsert currently only supports the arcadedb backend")

    if not paper_mentions:
        return {"new_organisms": 0, "new_genes": 0, "new_compounds": 0, "new_mention_edges": 0}

    entities_by_type: dict[str, dict[str, EntityMention]] = {"Organism": {}, "Gene": {}, "Compound": {}}
    edge_rows_by_type: dict[str, set[tuple[str, str]]] = {"Organism": set(), "Gene": set(), "Compound": set()}

    for paper_id, mentions in paper_mentions.items():
        for m in mentions:
            entities_by_type[m.vertex_type][m.entity_id] = m
            edge_rows_by_type[m.vertex_type].add((paper_id, m.entity_id))

    stats = {"new_organisms": 0, "new_genes": 0, "new_compounds": 0, "new_mention_edges": 0, "genes_named": 0}

    for vertex_type, key_prop in _KEY_PROP.items():
        # Upsert entities by types
        entities = list(entities_by_type[vertex_type].values())
        if entities:
            entity_params = [{"entity_id": e.entity_id, "name": e.name} for e in entities]
            new_count = arcadedb_http.run_script(_upsert_entities_sql(vertex_type, key_prop), entities=entity_params)[
                0
            ]["value"]
            stats[_STAT_KEY[vertex_type]] = new_count

            # _upsert_entities_sql only writes `name` on INSERT, so a Gene the GAF or
            # Oryzabase loader created key-only keeps a null name forever even once a
            # paper names it. Fill those in -- null-only, so nothing already set is
            # overwritten.
            if vertex_type == "Gene":
                stats["genes_named"] += backfill_gene_names(
                    {e.entity_id: e.name for e in entities if e.name}
                )

        # Once entities are created as nodes, add edges to them
        edges = edge_rows_by_type[vertex_type]
        if edges:
            edge_params = [{"paper_id": p, "entity_id": e} for p, e in edges]
            new_edges = arcadedb_http.run_script(
                _upsert_mentions_sql(vertex_type, key_prop, source), mentions=edge_params
            )[0]["value"]
            stats["new_mention_edges"] += new_edges

    return stats


def mark_papers_checked(paper_ids: list[str], checked_at: datetime) -> None:
    """Record that PubTator3 has been queried for these papers, whether or not any
    mentions survived the filter -- lets the pipeline's "unprocessed" query skip
    already-checked papers on the next run instead of re-fetching them forever.
    """
    if not paper_ids:
        return
    run_write(_MARK_CHECKED, paper_ids=paper_ids, checked_at=checked_at.isoformat())


def upsert_pathways(pathways: list[Pathway]) -> int:
    """Upsert a batch of Pathway nodes (from GO's biological_process branch, and
    Reactome). Returns the count of newly created nodes."""
    if not pathways:
        return 0
    params = [{"pathway_id": p.pathway_id, "name": p.name, "source_db": p.source_db} for p in pathways]
    return run_write(_UPSERT_PATHWAYS, pathways=params)[0]["new_pathways"]


# Same shape and same reasoning as _UPSERT_PATHWAYS -- Trait nodes never touch Paper.
_UPSERT_TRAITS = """
UNWIND $traits AS t
MERGE (tr:Trait {trait_id: t.trait_id})
ON CREATE SET tr._is_new = true
WITH tr, t, coalesce(tr._is_new, false) AS is_new
REMOVE tr._is_new
SET tr.name = t.name, tr.source_db = t.source_db
RETURN count(CASE WHEN is_new THEN 1 END) AS new_traits
"""


def upsert_traits(traits: list[Trait]) -> int:
    """Upsert a batch of Trait nodes (from the Trait Ontology). Returns the count of
    newly created nodes."""
    if not traits:
        return 0
    params = [{"trait_id": t.trait_id, "name": t.name, "source_db": t.source_db} for t in traits]
    return run_write(_UPSERT_TRAITS, traits=params)[0]["new_traits"]


# MATCHes the Trait rather than MERGEing it, unlike _UPSERT_PARTICIPATES_IN's treatment
# of Pathway: a TO id absent from the graph means trait_ontology.py hasn't run (or the
# id is obsolete/imported and was filtered out), and MERGEing would create a nameless
# Trait node that silently launders that mistake into the graph. The Gene *is* MERGEd,
# for the same reason PARTICIPATES_IN does it -- most trait-annotated rice genes have no
# node yet, since MENTIONS only creates one when literature happens to name it.
_UPSERT_ASSOCIATED_WITH = """
UNWIND $edges AS e
MERGE (g:Gene {gene_id: e.gene_id})
WITH g, e
MATCH (tr:Trait {trait_id: e.trait_id})
MERGE (g)-[edge:ASSOCIATED_WITH]->(tr)
ON CREATE SET edge._is_new = true
SET edge.source_db = e.source_db
WITH edge, coalesce(edge._is_new, false) AS is_new
REMOVE edge._is_new
RETURN count(CASE WHEN is_new THEN 1 END) AS new_edges
"""


def upsert_associated_with(edges: list[AssociatedWith]) -> int:
    """Upsert ASSOCIATED_WITH edges (Gene -> Trait, currently from Oryzabase). Returns
    the count of newly created edges."""
    if not edges:
        return 0
    params = [{"gene_id": e.gene_id, "trait_id": e.trait_id, "source_db": e.source_db} for e in edges]
    return run_write(_UPSERT_ASSOCIATED_WITH, edges=params)[0]["new_edges"]


# Plain Cypher/Bolt MERGE -- never touches Paper, so no vector-index-bug risk (same
# reasoning as _UPSERT_PATHWAYS above). MERGEs the Gene node too (not just MATCH): most
# of Reactome's ~12K human genes don't have a Gene node yet (PubTator3 MENTIONS only
# creates one when a paper happens to mention that gene), and gating pathway data on
# literature-processing catching up first would leave this mostly a no-op today. A
# Reactome-bootstrapped Gene node has no `name` yet (Reactome's file doesn't give gene
# symbols) -- MENTIONS will fill it in later if/when the literature catches up, keyed
# on the same gene_id.
_UPSERT_PARTICIPATES_IN = """
UNWIND $edges AS e
MERGE (g:Gene {gene_id: e.gene_id})
WITH g, e
MATCH (pw:Pathway {pathway_id: e.pathway_id})
MERGE (g)-[edge:PARTICIPATES_IN]->(pw)
ON CREATE SET edge._is_new = true
SET edge.evidence_code = e.evidence_code
WITH edge, coalesce(edge._is_new, false) AS is_new
REMOVE edge._is_new
RETURN count(CASE WHEN is_new THEN 1 END) AS new_edges
"""


def upsert_participates_in(edges: list[ParticipatesIn]) -> int:
    """Upsert PARTICIPATES_IN edges (Gene -> Pathway, currently from Reactome).
    Returns the count of newly created edges."""
    if not edges:
        return 0
    params = [{"gene_id": e.gene_id, "pathway_id": e.pathway_id, "evidence_code": e.evidence_code} for e in edges]
    return run_write(_UPSERT_PARTICIPATES_IN, edges=params)[0]["new_edges"]


# Plain Cypher/Bolt MERGE -- same reasoning as _UPSERT_PARTICIPATES_IN: never touches
# Paper, so no vector-index-bug risk. MERGEs the Compound node too, but only ever under
# the mesh: namespace already used by PubTator-sourced Compounds -- the crosswalk (see
# ingest/chebi_mesh_crosswalk.py) is what makes this safe; extract_produces() never
# passes a bare ChEBI id here; unresolved ones were already dropped upstream.
_UPSERT_PRODUCES = """
UNWIND $edges AS e
MERGE (c:Compound {compound_id: e.compound_id})
WITH c, e
MATCH (pw:Pathway {pathway_id: e.pathway_id})
MERGE (pw)-[edge:PRODUCES]->(c)
ON CREATE SET edge._is_new = true
SET edge.evidence_code = e.evidence_code
WITH edge, coalesce(edge._is_new, false) AS is_new
REMOVE edge._is_new
RETURN count(CASE WHEN is_new THEN 1 END) AS new_edges
"""


def upsert_produces(edges: list[Produces]) -> int:
    """Upsert PRODUCES edges (Pathway -> Compound, currently from Reactome via the
    ChEBI<->MeSH crosswalk). Returns the count of newly created edges."""
    if not edges:
        return 0
    params = [{"pathway_id": e.pathway_id, "compound_id": e.compound_id, "evidence_code": e.evidence_code} for e in edges]
    return run_write(_UPSERT_PRODUCES, edges=params)[0]["new_edges"]


# Sets `name` only WHERE it is currently null, never overwriting one that exists -- the
# additive-only discipline docs/plant_schema.md requires for vertices another job may write.
# Gene carries no vector index (that's Paper), so plain Cypher is safe here.
_BACKFILL_GENE_NAMES = """
UNWIND $genes AS g
MATCH (n:Gene {gene_id: g.gene_id})
WHERE n.name IS NULL
SET n.name = g.name
RETURN count(n) AS named
"""


def backfill_gene_names(names: dict[str, str]) -> int:
    """Give a readable symbol to Gene nodes that have none.

    Genes bootstrapped by the GAF and Oryzabase loaders are created key-only, because those
    sources are keyed on locus ids and carry no symbol -- so a trait query returns
    `gene: null` for most rows even when the graph knows the gene perfectly well. The
    gazetteer does know the symbol, so this closes the gap. Returns the count named.
    """
    if not names:
        return 0
    params = [{"gene_id": k, "name": v} for k, v in names.items()]
    return run_write(_BACKFILL_GENE_NAMES, genes=params)[0]["named"]


_READ_GENE_NAMES = """
MATCH (n:Gene)
WHERE n.name IS NOT NULL
RETURN n.gene_id AS gene_id, n.name AS name
"""

# Unlike _BACKFILL_GENE_NAMES this SETs unconditionally, so it must only ever be handed
# genes whose current name the caller has read and confirmed is a positional fallback.
# The guard lives in the caller (pipeline.run_gene_name_backfill) rather than in Cypher
# because deciding "is this a locus id" is a regex judgement, and ArcadeDB's Cypher layer
# has no dependable regex predicate to express it.
_UPGRADE_GENE_NAMES = """
UNWIND $genes AS g
MATCH (n:Gene {gene_id: g.gene_id})
SET n.name = g.name
RETURN count(n) AS upgraded
"""


def read_gene_names() -> dict[str, str]:
    """Every Gene node's current display name, for deciding which are safe to upgrade."""
    return {row["gene_id"]: row["name"] for row in run_read(_READ_GENE_NAMES)}


def upgrade_gene_names(names: dict[str, str]) -> int:
    """Replace a Gene's display name, for locus-id fallbacks that a curated symbol supersedes.

    Overwrites, so callers must have verified via ``read_gene_names`` that each current name
    is a bare locus id (``gene_crosswalk.is_locus_id``) -- never a curator- or
    extractor-assigned symbol. Returns the count replaced.
    """
    if not names:
        return 0
    params = [{"gene_id": k, "name": v} for k, v in names.items()]
    return run_write(_UPGRADE_GENE_NAMES, genes=params)[0]["upgraded"]
