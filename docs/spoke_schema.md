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
| PubTator3 | `Organism`/`Gene`/`Compound`/`Disease` nodes + `MENTIONS` edges from `Paper` | species-agnostic entity extraction from paper text |
| GO (`biological_process` branch) | `Pathway` nodes | `GO:` verbatim |
| Reactome (human only) | `Pathway` nodes, `PARTICIPATES_IN`, `PRODUCES`, `MAPS_TO` (to GO `Pathway`) | `R-HSA-` verbatim; genes `ncbigene:`; compounds via crosswalk |
| Disease Ontology (`doid.obo`) | `Disease.doid` + `IS_A` hierarchy, over MeSH-keyed `Disease` nodes | `DOID:` rides as a property; key stays `mesh:` |
| NCBI `gene_info` | LocusTag -> `ncbigene:` crosswalk substrate | — |
| ChEBI + MeSH + Biomappings | ChEBI -> MeSH compound crosswalk (33.7% coverage) | — |

Reactome ships more than litgraph currently reads. Files already downloaded, only
partly used:

| File | Used for |
|---|---|
| `ReactomePathways.txt` | `Pathway` nodes (human only) |
| `NCBI2Reactome.txt` | `PARTICIPATES_IN` |
| `ChEBI2Reactome.txt` | `PRODUCES`, via the ChEBI<->MeSH crosswalk |
| `Pathways2GoTerms_human.txt` | `MAPS_TO` (Reactome `Pathway` -> GO `Pathway`, same concept) |
| `Reactome2OMIM.txt` | **not a disease file despite the name** — confirmed live (2026-08-13): its header is `ACC, PathwayId, Pathway` and `ACC` values are UniProt accessions (e.g. `O00159-3`), not OMIM ids. It's a protein->pathway mapping, redundant with `NCBI2Reactome.txt`-derived `PARTICIPATES_IN`. Carries no disease identifier at all. |
| `HumanDiseasePathways.txt` | unused — just a `(pathway_id, name)` list flagging which existing `Pathway` nodes fall under Reactome's "Disease" top-level category; it does not link to a `Disease` node |

litgraph's entity extraction (PubTator3 mining paper text) has no SPOKE analog — SPOKE
is built entirely from curated/measured sources, not text mining.

## Closing the gap with SPOKE

In rough order of what unlocks the most:

- **Disease linkage.** The `Disease` node exists (MeSH-keyed, DOID + `IS_A` hierarchy
  from Disease Ontology, `MENTIONS` from PubTator3), but nothing connects it to
  `Pathway`/`Gene`/`Compound` beyond co-mention. Reactome's own files don't help here
  (see the table above) — `doid.obo` also carries zero `OMIM:` xrefs (confirmed live),
  so there's no free OMIM->MeSH bridge sitting in data already fetched. The real path
  is OMIM's `mim2gene.txt` (free) joined to existing `PARTICIPATES_IN`/`Gene` data,
  but landing that on a MeSH-keyed `Disease` node needs an OMIM->MeSH crosswalk —
  realistically a UMLS Metathesaurus license (free for research, but a registration
  step, not a drop-in download like the ChEBI<->MeSH crosswalk). DisGeNET is the other
  candidate, gene/variant -> disease, same crosswalk problem.
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
