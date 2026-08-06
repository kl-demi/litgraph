"""Biology vertex/edge types, declared into litgraph's shared schema registry."""

from litgraph.db.registry import EdgeType, NodeType, Prop, register

# Importing this registers the core paper types, which MENTIONS' `src="Paper"` depends on,
# and re-exports the shared ensure_schema() so callers need only this module.
from litgraph.db.schema import ensure_schema as ensure_schema


ORGANISM = NodeType("Organism", key="taxon_id", props=(Prop("name"),))

GENE = NodeType("Gene", key="gene_id", props=(Prop("name"), Prop("locus_id", indexed=True)))

COMPOUND = NodeType("Compound", key="compound_id", props=(Prop("name"),))

# PubtatorChecked is a bookkeeping node, marking "PubTator3 has already been queried for this 
# paper". Kept as its own node rather than a Paper property so this never has to write to a Paper vertex
# (see upsert.py's docstring on the ArcadeDB vector-index bug)
PUBTATOR_CHECKED = NodeType("PubtatorChecked", key="paper_id")

PATHWAY = NodeType("Pathway", key="pathway_id", props=(Prop("name"), Prop("source_db")))

TRAIT = NodeType("Trait", key="trait_id", props=(Prop("name"), Prop("source_db")))

MENTIONS        = EdgeType("MENTIONS", src="Paper", dst="Gene", props=(Prop("source"),))
PARTICIPATES_IN = EdgeType("PARTICIPATES_IN", src="Gene", dst="Pathway", props=(Prop("evidence_code"),))
PRODUCES        = EdgeType("PRODUCES", src="Pathway", dst="Compound", props=(Prop("evidence_code"),))
ASSOCIATED_WITH = EdgeType("ASSOCIATED_WITH", src="Gene", dst="Trait", props=(Prop("source_db"),))

register(
    ORGANISM,
    GENE,
    COMPOUND,
    PUBTATOR_CHECKED,
    PATHWAY,
    TRAIT,
    MENTIONS,
    PARTICIPATES_IN,
    PRODUCES,
    ASSOCIATED_WITH,
)
