# SPOKE-Adjacent Data Sources

Tracks litgraph's bio data sources against [SPOKE](https://spoke.ucsf.edu)'s, as a
reference point for what to add next. For how litgraph's schema/write/query layers
work, see `docs/architecture.md`.

## What SPOKE offers

SPOKE (Scalable Precision Medicine Open Knowledge Engine, UCSF) is a biomedical
knowledge graph: 27M nodes across 21 types and 53M edges across 55 types as published,
rebuilt weekly from 41 source databases
([Morris et al. 2023](https://academic.oup.com/bioinformatics/article/39/2/btad080/7033465)).

Ten of its sources carry most of the weight, grouped by the kind of claim they hold:

| Source | Kind of claim | Gives SPOKE a... |
|---|---|---|
| NCBI Gene | identity | permanent gene id, cross-referenced everywhere else |
| UniProt | identity | protein-level entity, separate from its gene |
| Disease Ontology | identity | disease vocabulary, cross-linked to clinical codes |
| Gene Ontology | annotation | shared vocabulary for what a gene does |
| Reactome | mechanism | step-by-step pathways with direction and evidence |
| STRING | network | protein-protein functional association scores |
| ChEMBL | measurement | molecule-vs-target lab measurements |
| DrugCentral | regulatory | which molecules are approved, and for what |
| SIDER | observed outcome | drug -> side-effect pairs from product labels |
| DisGeNET | statistical association | gene/variant -> disease links |

Confirmed overlap with litgraph today: **Gene Ontology**, **Reactome**, **MeSH**, and
**NCBI/Entrez Gene** (litgraph's `ncbigene:` key) are all listed sources on
`spoke.ucsf.edu/data-tools`.

## What litgraph already has

| Source | Yields | Native key |
|---|---|---|
| PubTator3 | `Organism`/`Gene`/`Compound` nodes + `MENTIONS` edges from `Paper` | species-agnostic entity extraction from paper text |
| GO (`biological_process` branch) | `Pathway` nodes | `GO:` verbatim |
| Reactome (human only) | `Pathway` nodes, `PARTICIPATES_IN`, `PRODUCES` | `R-HSA-` verbatim; genes `ncbigene:`; compounds via crosswalk |
| NCBI `gene_info` | LocusTag -> `ncbigene:` crosswalk substrate | — |
| ChEBI + MeSH + Biomappings | ChEBI -> MeSH compound crosswalk (33.7% coverage) | — |

Reactome ships more than litgraph currently reads. Files already downloaded, only
partly used:

| File | Used for |
|---|---|
| `ReactomePathways.txt` | `Pathway` nodes (human only) |
| `NCBI2Reactome.txt` | `PARTICIPATES_IN` |
| `ChEBI2Reactome.txt` | `PRODUCES`, via the ChEBI<->MeSH crosswalk |
| `Pathways2GoTerms_human.txt` | unused — would bridge a Reactome `Pathway` to the GO `Pathway` for the same concept |
| `Reactome2OMIM.txt`, `HumanDiseasePathways.txt` | unused — human disease linkage |

litgraph's entity extraction (PubTator3 mining paper text) has no SPOKE analog — SPOKE
is built entirely from curated/measured sources, not text mining.

## Closing the gap with SPOKE

In rough order of what unlocks the most:

- **Disease.** No disease node or disease linkage at all today. Cheapest first step:
  the two unused Reactome files above, before reaching for a new source. Real next
  source: DisGeNET (gene/variant -> disease) or OMIM.
- **Drug / PharmacologicClass.** litgraph's `Compound` is MeSH-keyed and chemical-only
  — no notion of an approved drug, dose, or drug class. Candidate sources: DrugCentral
  (approved drugs + indications) or DrugBank.
- **Protein.** litgraph collapses gene and protein into one `Gene` node. No
  protein-level data (interactions, structure, subcellular location). Candidate:
  UniProt.
- **Anatomy.** No tissue/organ localization for any gene or pathway. Candidate: Uberon.
- **SideEffect.** No adverse-event data. Candidate: SIDER.
- **Broader compound evidence.** ChEMBL/BindingDB carry lab measurements (binding
  affinity, assay conditions) litgraph doesn't have for any compound.

Stage one source at a time against real downloaded data before writing an edge — same
discipline as the ChEBI<->MeSH crosswalk (see `docs/architecture.md` §9). A source
proposed above but not yet built is not yet trustworthy; it's a candidate, not a plan.
