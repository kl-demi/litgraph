from datetime import datetime

from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_write
from spokebio.models import EntityMention, Pathway

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
def _upsert_mentions_sql(vertex_type: str, key_prop: str) -> str:
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
      CREATE EDGE MENTIONS FROM $paperRid TO $entityRid;
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
# Paper at all in this pass (no Gene/Compound membership edges yet, see
# docs/plant_schema.md), so there's no vector-index-bug risk to route around.
_UPSERT_PATHWAYS = """
UNWIND $pathways AS p
MERGE (pw:Pathway {pathway_id: p.pathway_id})
ON CREATE SET pw._is_new = true
WITH pw, p, coalesce(pw._is_new, false) AS is_new
REMOVE pw._is_new
SET pw.name = p.name, pw.source_db = p.source_db
RETURN count(CASE WHEN is_new THEN 1 END) AS new_pathways
"""


def upsert_mentions(paper_mentions: dict[str, list[EntityMention]]) -> dict[str, int]:
    """Upsert Gene/Compound/Organism nodes and MENTIONS edges for a batch of papers.

    ``paper_mentions`` maps litgraph Paper.id -> the mentions PubTator3 found for it
    (an empty list means no edges). Returns counts of newly created nodes/edges.
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

    stats = {"new_organisms": 0, "new_genes": 0, "new_compounds": 0, "new_mention_edges": 0}

    for vertex_type, key_prop in _KEY_PROP.items():
        # Upsert entities by types
        entities = list(entities_by_type[vertex_type].values())
        if entities:
            entity_params = [{"entity_id": e.entity_id, "name": e.name} for e in entities]
            new_count = arcadedb_http.run_script(_upsert_entities_sql(vertex_type, key_prop), entities=entity_params)[
                0
            ]["value"]
            stats[_STAT_KEY[vertex_type]] = new_count

        # Once entities are created as nodes, add edges to them
        edges = edge_rows_by_type[vertex_type]
        if edges:
            edge_params = [{"paper_id": p, "entity_id": e} for p, e in edges]
            new_edges = arcadedb_http.run_script(_upsert_mentions_sql(vertex_type, key_prop), mentions=edge_params)[
                0
            ]["value"]
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
    """Upsert a batch of Pathway nodes (from GO's biological_process branch, and later
    PlantCyc/MetaCyc). Returns the count of newly created nodes."""
    if not pathways:
        return 0
    params = [{"pathway_id": p.pathway_id, "name": p.name, "source_db": p.source_db} for p in pathways]
    return run_write(_UPSERT_PATHWAYS, pathways=params)[0]["new_pathways"]
