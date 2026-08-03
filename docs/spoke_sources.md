# Credibility of SPOKE sources

## SPOKE Overview

[SPOKE](https://spoke.ucsf.edu) (the Scalable Precision Medicine Open Knowledge Engine,
built at UCSF) is a biomedical knowledge graph: a single connected map of genes,
proteins, drugs, diseases, tissues, and the relationships between them. As published in
2023 it held 27 million entities of 21 kinds and 53 million relationships of 55 kinds,
assembled from 41 separate databases ([Morris et al., *Bioinformatics*
2023](https://academic.oup.com/bioinformatics/article/39/2/btad080/7033465)); more
recent work using it reports it has since grown past 42 million entities and 160 million
relationships ([Soman et al., *Bioinformatics*
2024](https://academic.oup.com/bioinformatics/article/40/9/btae560/7759620)).

The obvious question for anyone deciding whether to build on it is: **where did all
those claims come from, and who checked them?** 

---

## On SPOKE's trustability

**1. SPOKE re-publishes claims with attribution.**
Every relationship in the graph carries the name of the database it came from. Therefore, SPOKE's trustworthiness is dependent on the trustworthiness of its 41 sources. Most of those sources are long-running community resources
that publish their own methods in the peer-reviewed literature (the annual *Nucleic
Acids Research* database issue) and are themselves cited thousands of times a year.

**2. It is built from human-curated and experimentally measured data, not from
automated reading of papers.** This matters because text mining, ie. having software read
millions of abstracts and guess at relationships, is the cheapest way to make a large
knowledge graph and the least reliable. SPOKE mostly declines that shortcut.

**3. Predictions built on this kind of graph have been validated against data held back
from the model.** SPOKE's direct predecessor, Hetionet, integrated 29 resources and
scored 209,168 drug–disease pairs for whether the drug might treat the disease; the
predictions held up against two independent sets of real treatments that were not used
in training ([Himmelstein et al., *eLife*
2017](https://elifesciences.org/articles/26726)). SPOKE itself has been used to embed
millions of de-identified patient records onto the graph, where it recovered the correct
anatomical sites of multiple sclerosis without being told them ([Nelson et al., *Nature
Communications* 2019](https://www.nature.com/articles/s41467-019-11069-0)) and later
detected early warning features of MS in patient records years before diagnosis
([Nelson et al., *JAMIA*
2022](https://academic.oup.com/jamia/article/29/3/424/6463510)).

**4. It is refreshed continuously.** SPOKE re-downloads and rebuilds from its sources
weekly to keep up-to-date.

---

## Table 1 — Kinds of statement in each source

Sources differ by the kind of statement they contain, not by how much of the same data
they cover.

| Kind of statement | Example claim | Sources |
|---|---|---|
| **Identity** | "This gene has the official symbol *TP53* and the permanent ID 7157." | NCBI Gene, UniProt, Disease Ontology |
| **Annotation** | "*TP53* is involved in programmed cell death" — the label drawn from a fixed vocabulary. | Gene Ontology |
| **Mechanism** | "This complex cleaves this substrate in the nucleus, releasing this product." | Reactome |
| **Measurement** | "This compound inhibited this protein at 25 nM in the assay reported in this paper." | ChEMBL, BindingDB |
| **Regulatory fact** | "This drug is approved for this indication; its efficacy target is this protein." | DrugCentral, DrugBank |
| **Observed human outcome** | "Patients taking this drug reported swelling around the eyes." | SIDER |
| **Statistical association** | "Variation in this gene is associated with this disease, at this strength." | DisGeNET, GWAS Catalog, OMIM |
| **Context** | "This gene is expressed in liver tissue and not in muscle." | Bgee, TISSUES, Human Protein Atlas |
| **Network** | "These two proteins are functionally associated; confidence 0.9 of 1." | STRING |

The three drug-related sources illustrate the distinction. ChEMBL records what happened
when a molecule was tested against a protein in the lab. DrugCentral records which
molecules a regulator has approved for human use, and for what. SIDER records what was
then observed in patients. These are different evidence types with different failure modes.

---

## Table 2 — SPOKE's 10 major sources

Each of these ten represents a distinct kind of evidence. SPOKE draws on 41 sources in
total; the rest are largely additional entries in the same categories (see [Sources not
covered above](#sources-not-covered-above)).

### Identity

| Source | Contents | Distinct contribution | Provenance and caveats |
|---|---|---|---|
| **NCBI Gene** ([site](https://www.ncbi.nlm.nih.gov/gene)) | One record per gene per species: official symbol, alternative names, chromosomal position, and links to other databases. | The permanent identifier for every gene. Makes no biological claims; it is the registry that lets other databases refer to the same gene unambiguously. | Maintained by the US National Library of Medicine, tied to a curated reference set of gene and protein sequences. Public domain. |
| **UniProt** ([site](https://www.uniprot.org)) | One record per protein: amino-acid sequence, functional regions, subcellular location, chemical modifications, and a summary of function, each statement cited to a paper. | The protein layer. SPOKE models genes and proteins as separate entities, since one gene can yield several protein forms and drugs bind proteins rather than genes. | Two tiers with different reliability: Swiss-Prot (~570,000 entries) is curated by hand; TrEMBL (hundreds of millions) is automatically generated and unreviewed. ([UniProt Consortium, *NAR* 2025, 53:D609](https://academic.oup.com/nar/article/53/D1/D609/7902999)) |
| **Disease Ontology** ([site](https://disease-ontology.org)) | ~11,000 human diseases in a parent/child hierarchy, each cross-linked to the equivalent term in the major medical coding systems. | The disease vocabulary. Lets a genetics result and a clinical code refer to the same illness. Contains no biological findings. | Expert-curated, continuously updated, public domain (CC0). Its terms have been applied to over 1.5 million biomedical records and citations. ([Schriml et al., *NAR* 2022, 50:D1255](https://academic.oup.com/nar/article/50/D1/D1255/6424774)) |

### Function and mechanism

| Source | Contents | Distinct contribution | Provenance and caveats |
|---|---|---|---|
| **Gene Ontology** ([site](https://geneontology.org)) | A fixed vocabulary of ~40,000 biological activities and processes, plus files linking genes to the terms that apply to them. | A shared label set. Every lab describes a given process with the same term, making results comparable across species and studies. States that a gene is involved in something, not how. | International consortium. Each gene–term link carries an evidence code recording how it was determined, from direct experiment to automated inference, so annotations can be filtered by evidence type. ([GO Consortium, *NAR* 2026, 54:D1779](https://academic.oup.com/nar/article/54/D1/D1779/8383826)) |
| **Reactome** ([site](https://reactome.org)) | Step-by-step biochemical pathways: inputs, outputs, catalysts, cellular compartment, and how steps nest into larger processes. | Mechanism with direction and ordering, rather than membership alone. | Each pathway is written by a curator with an external domain expert, then peer-reviewed by a second external expert before release. Open access, open source. ([Milacic et al., *NAR* 2024, 52:D672](https://academic.oup.com/nar/article/52/D1/D672/7369850)) |
| **STRING** ([site](https://string-db.org)) | Scores from 0 to 1 for functional association between protein pairs, broken out by evidence channel: experiments, curated pathways, co-expression, cross-genome patterns, text mentions. | Coverage of the whole network, including regions no curator has written up. | The most inferential source here. Many links are computed rather than observed, but scores are calibrated against known pathways and decomposed by channel, and low-throughput experiments score higher than high-throughput ones. ≥0.7 is the conventional high-confidence threshold. ([Szklarczyk et al., *NAR* 2023, 51:D638](https://academic.oup.com/nar/article/51/D1/D638/6825349)) |

### Drugs

| Source | Contents | Distinct contribution | Provenance and caveats |
|---|---|---|---|
| **ChEMBL** ([site](https://www.ebi.ac.uk/chembl/)) | ~20.8 million measurements of molecule activity against biological targets, across ~2.4 million molecules and ~1.6 million assays (release 35), each cited to its publication. | The experimental evidence layer for chemistry: measured values under stated conditions, with the source paper attached. | Curated by hand at EMBL-EBI from peer-reviewed medicinal-chemistry journals. Openly licensed, fully downloadable. Main caveat is comparability rather than reliability: values from different assay setups are not directly comparable. ([Zdrazil et al., *NAR* 2024, 52:D1180](https://academic.oup.com/nar/article/52/D1/D1180/7337608)) |
| **DrugCentral** ([site](https://drugcentral.org)) | Active ingredients of approved medicines: approval status, licensed indications, efficacy target, mechanism of action, and pharmacologic class. | The approved-drug subset. Of ChEMBL's millions of molecules, a few thousand are prescribable; this is that list with its licensed uses. | Curated from regulatory filings and approved product labels. Freely downloadable, no registration. ([Avram et al., *NAR* 2023, 51:D1276](https://academic.oup.com/nar/article/51/D1/D1276/6885038)) |
| **SIDER** ([site](http://sideeffects.embl.de)) | 139,756 drug–side-effect pairs across 1,430 medicines and 5,868 adverse effects, extracted from approved product labels. | Adverse effects observed in patients. | Authoritative underlying documents, but extraction is automated rather than manual, so extraction errors are possible. Not updated since October 2015, so newer drugs are absent. License restricts commercial use. ([Kuhn et al., *NAR* 2016, 44:D1075](https://academic.oup.com/nar/article/44/D1/D1075/2502602)) |

### Disease and body context

| Source | Contents | Distinct contribution | Provenance and caveats |
|---|---|---|---|
| **DisGeNET** ([site](https://disgenet.com)) | Gene–disease and variant–disease associations pooled from population studies, inherited-disease catalogues, curated databases, and automated literature extraction, each with a confidence score. | The link between the molecular and clinical halves of the graph. Without it, disease entities have no biological connections. | An aggregator: reliability varies per row, and the source field distinguishes curated entries from text-extracted ones. Now under a licensed model; the free academic tier requires institutional affiliation and excludes full download. ([Piñero et al., *NAR* 2020, 48:D845](https://academic.oup.com/nar/article/48/D1/D845/5611674)) |
| **Bgee** ([site](https://www.bgee.org)) | Gene × body-part × developmental-stage expression calls, standardised so anatomical terms mean the same thing across species and experiments. | Where in the body a gene is active, which constrains which mechanisms are plausible in a given tissue. | Built by re-processing public experimental data through one consistent published pipeline rather than adopting each study's own analysis, which is what makes calls comparable. Calls are statistical, with quality levels attached. Public domain (CC0). |

---

## Evidence tiers

| Tier | Definition | Sources |
|---|---|---|
| **Curated and independently peer-reviewed** | Written by an expert curator, checked by a second external expert before release. | Reactome |
| **Expert-curated, internally reviewed** | Curators extract findings from primary literature under a published protocol with internal QC. | UniProt (Swiss-Prot), ChEMBL, Gene Ontology (experimental evidence codes), Disease Ontology, NCBI Gene |
| **Primary regulatory document** | Transcribed from documents carrying external legal or regulatory weight. | DrugCentral, SIDER's underlying labels |
| **Computed with calibrated confidence** | Machine-generated, with scores benchmarked against known-true cases and published. | STRING, Bgee, Gene Ontology's inferred annotations |
| **Automatically extracted from text** | Software-extracted claims. A minority of SPOKE. | SIDER's extraction step, part of DisGeNET |
| **Aggregated** | Re-published from other sources; reliability is inherited per row, not per file. | DisGeNET |

---

## SPOKE Limitations

- **Some sources are frozen.** SIDER stopped updating in 2015.
- **Some are no longer openly available.** DisGeNET moved behind a license after SPOKE
  incorporated it.
- **Absence is not evidence of absence.** A missing drug–disease link usually means
  nothing has been published, not that the two are unrelated. Coverage is biased toward
  well-studied genes and common diseases.
- **Cross-database identifier matching is lossy.** Databases label the same chemical
  under different ID systems, and translation between them is never complete. Coverage
  for one such translation in this project sits in the 30–50% range (see
  [`spoke_schema.md`](spoke_schema.md)). Unmatched entries are dropped silently.
- **Measurements are not comparable across experiments.** Two ChEMBL values for the same
  drug–protein pair can differ by orders of magnitude for legitimate experimental
  reasons.

---

## Sources not covered above

The remaining 31 sources mostly add depth to categories already listed:

- **DrugBank** — same claim type as DrugCentral. SPOKE pins an older version, as current
  releases require a commercial license.
- **BindingDB** — same claim type as ChEMBL, narrower scope.
- **OMIM, GWAS Catalog, DISEASES, DOAF, DistiLD** — DisGeNET's role, with different
  evidence mixes.
- **TISSUES, Human Protein Atlas** — Bgee's role.
- **Uberon, MeSH, Cell Ontology** — vocabulary and hierarchy resources like Disease
  Ontology, covering body parts, literature subject terms, and cell types.
- **WikiPathways, Pathway Commons, KEGG, IntAct, Pfam, InterPro, LINCS L1000, FooDB,
  CIViC, COSMIC, ClinicalTrials.gov** — each deepens one axis already described.

Full list: [spoke.ucsf.edu/data-tools](https://spoke.ucsf.edu/data-tools).

---

## Glossary

| Term | Meaning |
|---|---|
| **NCBI** | US National Center for Biotechnology Information, part of the National Library of Medicine. Runs PubMed and the main public sequence and gene databases. |
| **EMBL-EBI** | European Bioinformatics Institute, NCBI's European counterpart. Runs ChEMBL, and UniProt and Reactome jointly with others. |
| **Locus** | A gene's position: which chromosome it sits on and where along it. |
| **RefSeq** | NCBI's curated reference set of gene and protein sequences — one agreed sequence per gene, as distinct from the many variants submitted by individual labs. |
| **Curation** | A trained human reading primary literature and recording findings in a structured database under a documented protocol, as opposed to automated extraction. |
| **Ontology** | A controlled vocabulary with structure: a fixed term list plus stated relationships between terms ("X is a kind of Y", "X is part of Y"). |
| **Evidence code** | A tag on an individual annotation recording how it was determined — direct experiment, inference from a similar gene, or automated assignment. |
| **Swiss-Prot / TrEMBL** | UniProt's two halves: Swiss-Prot is human-reviewed, TrEMBL is machine-generated and unreviewed. |
| **Assay** | A single laboratory experiment testing what a molecule does to a biological target under specified conditions. |
| **Cross-reference (xref)** | A pointer stating that an entry in one database is the same thing as an entry in another. |
| **MeSH** | Medical Subject Headings, the National Library of Medicine's vocabulary for indexing biomedical literature. Every PubMed paper is tagged with MeSH terms. |
| **MedDRA / UMLS** | Two further medical vocabularies: MedDRA for adverse events in drug regulation, UMLS as an umbrella mapping many vocabularies together. |
| **GWAS** | Genome-wide association study: scanning many genomes for variants statistically linked to a trait or disease. Produces associations, not mechanisms. |

---

## Relevance to this project

LitGraph builds a graph in SPOKE's spirit by a different route: literature ingestion and
entity extraction, supplemented by direct loads from curated sources. Of the ten above,
Gene Ontology and Reactome are ingested today, and NCBI Gene identifiers are the graph's
gene keys. Schema, current status, and the reasoning behind source ordering are in
[`spoke_schema.md`](spoke_schema.md).
