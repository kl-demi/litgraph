"""Biology node/edge types, declared into litgraph's shared schema registry.

Usage: importing this module registers the types; call `ensure_schema()` (re-exported
below) to create them in the database alongside the core paper types.
"""

from litgraph.db.registry import EdgeType, NodeType, Prop, register

# The following import registers the core paper types and re-exports the shared 
# ensure_schema() so callers need only this module.
from litgraph.db.schema import ensure_schema as ensure_schema


# Entity nodes are bootstrappable: their ids are pre-validated (via PubTator normalization
# or a crosswalk), and a key-only node is complete. Ontology terms (Pathway) stay
# non-bootstrappable -- see NodeType.bootstrappable.
ORGANISM = NodeType("Organism", key="taxon_id", props=(Prop("name"),), bootstrappable=True)

GENE = NodeType("Gene", key="gene_id", props=(Prop("name"),), bootstrappable=True)

COMPOUND = NodeType("Compound", key="compound_id", props=(Prop("name"),), bootstrappable=True)

# MeSH-keyed, like Compound: PubTator3 normalizes diseases to MeSH descriptors, so a
# doid_id field holding a MeSH id would misrepresent the data. Disease Ontology maps only
# ~62% of them, so DOID rides along as a property -- see ingest/disease_ontology.py.
DISEASE = NodeType(
    "Disease", key="disease_id", props=(Prop("name"), Prop("doid", indexed=True)), bootstrappable=True
)

# Bookkeeping node, kept as its own node rather than a Paper property so this never
# has to write to a Paper vertex (see upsert.py's docstring on the ArcadeDB vector-index bug)
EXTRACTION_CHECKED = NodeType(
    "ExtractionChecked",
    key="check_id",  # "<extractor>:<paper_id>", so each extractor tracks its own coverage
    props=(Prop("extractor"), Prop("paper_id", indexed=True)),
)

PATHWAY = NodeType("Pathway", key="pathway_id", props=(Prop("name"), Prop("source_db")))

MENTIONS        = EdgeType(
    "MENTIONS", src="Paper", dst=("Organism", "Gene", "Compound", "Disease"), props=(Prop("source"),)
)
IS_A            = EdgeType("IS_A", src="Disease", dst="Disease")
PARTICIPATES_IN = EdgeType("PARTICIPATES_IN", src="Gene", dst="Pathway", props=(Prop("evidence_code"),))
PRODUCES        = EdgeType("PRODUCES", src="Pathway", dst="Compound", props=(Prop("evidence_code"),))
MAPS_TO         = EdgeType("MAPS_TO", src="Pathway", dst="Pathway")

register(
    ORGANISM,
    GENE,
    COMPOUND,
    DISEASE,
    EXTRACTION_CHECKED,
    PATHWAY,
    MENTIONS,
    IS_A,
    PARTICIPATES_IN,
    PRODUCES,
    MAPS_TO,
)
