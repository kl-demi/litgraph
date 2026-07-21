from pydantic import BaseModel


class EntityMention(BaseModel):
    """One normalized Gene/Compound/Organism mention found by PubTator3 in a paper."""

    vertex_type: str  # "Gene" | "Compound" | "Organism"
    entity_id: str  # namespaced natural key, e.g. "ncbigene:27161", "mesh:D000241", "9606"
    name: str
