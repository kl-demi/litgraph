from pydantic import BaseModel


class EntityMention(BaseModel):
    """One normalized Gene/Compound/Organism mention found by PubTator3 in a paper."""

    vertex_type: str  # "Gene" | "Compound" | "Organism"
    entity_id: str  # namespaced natural key, e.g. "ncbigene:27161", "mesh:D000241", "9606"
    name: str


class Pathway(BaseModel):
    """A biological process/pathway node -- species-agnostic ones from GO's
    biological_process branch (source_db="GO"), human-specific ones from Reactome
    (source_db="Reactome"). See docs/spoke_schema.md."""

    pathway_id: str
    name: str
    source_db: str


class ParticipatesIn(BaseModel):
    """One Gene -> Pathway membership claim (currently sourced from Reactome)."""

    gene_id: str  # namespaced, e.g. "ncbigene:7157" -- matches the existing Gene.gene_id key
    pathway_id: str  # bare native id, e.g. "R-HSA-111448"
    evidence_code: str  # e.g. "TAS", "IEA"
