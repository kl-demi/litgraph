from pydantic import BaseModel


class EntityMention(BaseModel):
    """One normalized Gene/Compound/Organism mention found by PubTator3 in a paper."""

    vertex_type: str  # "Gene" | "Compound" | "Organism"
    entity_id: str  # namespaced natural key, e.g. "ncbigene:27161", "mesh:D000241", "9606"
    name: str


class Pathway(BaseModel):
    """A biological process/pathway node -- species-agnostic ones from GO's
    biological_process branch (source_db="GO", pathway_id a GO id like "GO:0009611"),
    species-specific ones from PlantCyc/MetaCyc later (see docs/plant_schema.md)."""

    pathway_id: str
    name: str
    source_db: str
