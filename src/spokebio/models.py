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


class Trait(BaseModel):
    """A measurable trait from the Trait Ontology (TO) -- the named dimension being
    measured ("drought tolerance"), not an observed value. See docs/plant_schema.md's
    Trait row."""

    trait_id: str
    name: str
    source_db: str


class AssociatedWith(BaseModel):
    """One Gene -> Trait association claim (currently sourced from Oryzabase).

    No evidence_code, unlike ParticipatesIn: Oryzabase publishes no per-annotation
    evidence code, so ``source_db`` is the only provenance available -- inventing a
    code here would assert a confidence level the source doesn't state.
    """

    gene_id: str  # namespaced, e.g. "ncbigene:4326471"
    trait_id: str  # bare native id, e.g. "TO:0000276"
    source_db: str


class ParticipatesIn(BaseModel):
    """One Gene -> Pathway membership claim (currently sourced from Reactome)."""

    gene_id: str  # namespaced, e.g. "ncbigene:7157" -- matches the existing Gene.gene_id key
    pathway_id: str  # bare native id, e.g. "R-HSA-111448"
    evidence_code: str  # e.g. "TAS", "IEA"


class Produces(BaseModel):
    """One Pathway -> Compound production claim (currently sourced from Reactome, via
    the ChEBI<->MeSH crosswalk -- see ingest/chebi_mesh_crosswalk.py)."""

    pathway_id: str  # bare native id, e.g. "R-HSA-111448"
    compound_id: str  # namespaced, e.g. "mesh:D009569" -- matches the existing Compound.compound_id key
    evidence_code: str  # e.g. "TAS", "IEA"
