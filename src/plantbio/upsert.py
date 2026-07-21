from datetime import datetime

from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_write
from plantbio.models import EntityMention

_KEY_PROP = {"Organism": "taxon_id", "Gene": "gene_id", "Compound": "compound_id"}
_STAT_KEY = {"Organism": "new_organisms", "Gene": "new_genes", "Compound": "new_compounds"}

# Same shape as graph/upsert.py's _UPSERT_STUBS_SQL / _UPSERT_CITATION_EDGES_SQL
# (litgraph switched those to this SQL/HTTP pattern today, both for speed and because
# it never SETs a property on an existing Paper -- only SELECTs it to read @rid. That
# matters here specifically: any write that touches a Paper vertex tracked by the
# Paper[embedding] LSM_VECTOR index (SET-ting any field on an already-embedded Paper)
# fails at commit on ArcadeDB 26.7.1 (see memory: litgraph-arcadedb-vector-index-timer-
# bug). Mirroring the exact same pattern here means MENTIONS-edge writes carry the same
# already-proven-safe characteristics as the CITES-edge writes running in production
# right now, rather than reintroducing that risk with a fresh Cypher/Bolt MERGE.
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


def upsert_mentions(paper_mentions: dict[str, list[EntityMention]]) -> dict[str, int]:
    """Upsert Gene/Compound/Organism nodes and MENTIONS edges for a batch of papers.

    ``paper_mentions`` maps litgraph Paper.id -> the mentions PubTator3 found for it
    (an empty list is fine -- it just contributes no edges). Returns counts of newly
    created nodes/edges.
    """
    settings = get_settings()
    if settings.graph_backend != "arcadedb":
        raise NotImplementedError("plantbio upsert currently only supports the arcadedb backend")

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
        entities = list(entities_by_type[vertex_type].values())
        if entities:
            entity_params = [{"entity_id": e.entity_id, "name": e.name} for e in entities]
            new_count = arcadedb_http.run_script(_upsert_entities_sql(vertex_type, key_prop), entities=entity_params)[
                0
            ]["value"]
            stats[_STAT_KEY[vertex_type]] = new_count

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
    already-checked papers on the next run instead of re-fetching them forever. Plain
    Cypher/Bolt MERGE is fine here (unlike upsert_mentions above): PubtatorChecked is a
    brand-new node type with no vector index, so it can't trip the embedding-index
    commit bug that motivates the SQL/HTTP path for Paper-adjacent writes.
    """
    if not paper_ids:
        return
    run_write(_MARK_CHECKED, paper_ids=paper_ids, checked_at=checked_at.isoformat())
