from pydantic import BaseModel


class EntityMention(BaseModel):
    """One normalized Gene/Compound/Organism/Disease mention found by PubTator3 in a paper."""

    vertex_type: str  # "Gene" | "Compound" | "Organism" | "Disease"
    entity_id: str  # namespaced natural key, e.g. "ncbigene:27161", "mesh:D000241", "9606"
    name: str


class DiseaseXref(BaseModel):
    """One Disease Ontology term, keyed by the MeSH id it cross-references."""

    disease_id: str  # namespaced, e.g. "mesh:D003920" -- matches the existing Disease key
    doid: str  # e.g. "DOID:9352"
    name: str  # DO's label, which supersedes PubTator's mention-derived one


class DiseaseIsA(BaseModel):
    """One Disease -> Disease subtype claim, projected from DO's is_a hierarchy."""

    child_id: str  # namespaced, e.g. "mesh:D002289"
    parent_id: str  # namespaced, e.g. "mesh:D009369"


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


class Produces(BaseModel):
    """One Pathway -> Compound production claim (currently sourced from Reactome, via
    the ChEBI<->MeSH crosswalk -- see ingest/chebi_mesh_crosswalk.py)."""

    pathway_id: str  # bare native id, e.g. "R-HSA-111448"
    compound_id: str  # namespaced, e.g. "mesh:D009569" -- matches the existing Compound.compound_id key
    evidence_code: str  # e.g. "TAS", "IEA"


class PathwayGoMapping(BaseModel):
    """One Reactome Pathway -> GO Pathway correspondence for the same concept."""

    reactome_pathway_id: str  # e.g. "R-HSA-73843"
    go_pathway_id: str  # e.g. "GO:0006015" -- dropped at write time if outside GO's
    # biological_process branch, since only that branch gets a Pathway node (see ingest/go.py)
