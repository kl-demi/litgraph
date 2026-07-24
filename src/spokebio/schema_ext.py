from litgraph.config import get_settings
from litgraph.db import arcadedb_http

_VERTEX_TYPES = ["Organism", "Gene", "Compound", "PubtatorChecked", "Pathway"]
_EDGE_TYPES = ["MENTIONS"]

# (vertex_type, key_property) -- matches docs/plant_schema.md's node table, except
# Compound's key is named compound_id rather than chebi_id: see
# ingest/pubtator.py's module docstring for why. PubtatorChecked is a bookkeeping node
# (not a domain entity) marking "PubTator3 has already been queried for this paper",
# so re-runs don't keep re-fetching papers that legitimately had zero qualifying
# mentions -- kept as its own node rather than a Paper property so this never has to
# write to a Paper vertex (see upsert.py's docstring on the ArcadeDB vector-index bug).
# Pathway holds both GO's species-agnostic biological_process terms (source_db="GO")
# and, later, PlantCyc/MetaCyc's species-specific pathways (source_db="PlantCyc"/
# "MetaCyc") in the same node type/key, per docs/plant_schema.md's Pathway row.
_UNIQUE_KEYS = [
    ("Organism", "taxon_id"),
    ("Gene", "gene_id"),
    ("Compound", "compound_id"),
    ("PubtatorChecked", "paper_id"),
    ("Pathway", "pathway_id"),
]


def ensure_schema() -> None:
    """Idempotently create the vertex/edge types and indexes this module relies on, on
    top of litgraph's existing Paper/Author/Category schema. ArcadeDB-only for now,
    matching litgraph's own default backend and docs/plant_schema.md's scope.
    """
    settings = get_settings()
    if settings.graph_backend != "arcadedb":
        raise NotImplementedError("spokebio schema currently only supports the arcadedb backend")

    for vertex_type in _VERTEX_TYPES:
        arcadedb_http.ensure_ddl(f"CREATE VERTEX TYPE {vertex_type} IF NOT EXISTS")
    for edge_type in _EDGE_TYPES:
        arcadedb_http.ensure_ddl(f"CREATE EDGE TYPE {edge_type} IF NOT EXISTS")

    for vertex_type, key_prop in _UNIQUE_KEYS:
        arcadedb_http.ensure_ddl(f"CREATE PROPERTY {vertex_type}.{key_prop} STRING")
        arcadedb_http.ensure_ddl(f"CREATE INDEX ON {vertex_type} ({key_prop}) UNIQUE")
    for vertex_type in ("Organism", "Gene", "Compound", "Pathway"):
        arcadedb_http.ensure_ddl(f"CREATE PROPERTY {vertex_type}.name STRING")
    arcadedb_http.ensure_ddl("CREATE PROPERTY Pathway.source_db STRING")
