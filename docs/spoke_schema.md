# Human / SPOKE-Adjacent Schema

## Overview

LitGraph is moving from a plant-biology schema (`docs/plant_schema.md`, superseded
2026-07-24 and kept as historical record) to a **human** biomedical knowledge graph in
the spirit of [SPOKE](https://spoke.ucsf.edu) (Scalable Precision Medicine Open
Knowledge Engine — 27M nodes across 21 types, 53M edges across 55 types, built weekly
from 41 source databases; [Morris et al. 2023](https://academic.oup.com/bioinformatics/article/39/2/btad080/7033465)).

This schema is "adjacent" to SPOKE rather than a clone: it shares SPOKE's node/edge
vocabulary and has confirmed overlap in source databases (Reactome, Gene Ontology,
MeSH, and Entrez/NCBI Gene — all listed on `spoke.ucsf.edu/data-tools`), but differs in
construction strategy. SPOKE aggregates directly from curated databases; LitGraph's
PubMed ingestion pipeline additionally serves as the entity-extraction layer, via
PubTator3, mining the literature corpus itself for entities and relationships.

## Status

All work below lives in `src/spokebio/`.

| Component | Description | Implementation |
|---|---|---|
| `Organism` / `Gene` / `Compound` nodes, `MENTIONS` edges from `Paper` | Species-agnostic entity extraction from paper text via PubTator3. Keys: `Organism.taxon_id` (NCBI Taxonomy id), `Gene.gene_id` (`ncbigene:<id>`), `Compound.compound_id` (`mesh:<id>` — PubTator3 normalizes chemicals to MeSH, not ChEBI). `PubtatorChecked` is a bookkeeping node tracking which papers have already been queried, so bulk re-runs don't repeatedly re-fetch papers with zero qualifying mentions. Live in production. | `spokebio/ingest/pubtator.py`, `spokebio/pipeline.py::run_pubtator_mentions` |
| `Pathway` nodes from GO's `biological_process` branch | 24,129 non-obsolete terms ingested as of 2026-07-23. Species-agnostic — GO's ontology doesn't vary by organism. Keyed on GO's native id verbatim (e.g. `GO:0009611`), `source_db="GO"`. Live in production. | `spokebio/ingest/go.py`, `spokebio/pipeline.py::run_go_ingest` |
| Gene ID crosswalk utility | Downloads and parses NCBI's per-organism `gene_info` file into a `LocusTag -> ncbigene:<id>` map. Originally built for a TAIR/Arabidopsis-specific gap (see `docs/plant_schema.md`); not required for the Reactome integration below, since Reactome keys its gene-pathway file directly on NCBI Gene ID. Retained as a reusable utility for any future source that identifies genes by locus tag. Not on the critical path for anything currently live. | `spokebio/ingest/gene_crosswalk.py` |
| Reactome `Pathway` nodes + `PARTICIPATES_IN` edges | 2,883 human pathways, 49,714 Gene->Pathway edges. Creates bare `Gene` nodes on demand for referenced genes with no existing node (see Design Principle 5). Live in production as of 2026-07-27 (real query verified: e.g. `APC` (`ncbigene:324`) correctly linked to Wnt/β-catenin destruction-complex pathways). | `spokebio/ingest/reactome.py`, `spokebio/pipeline.py::run_reactome_ingest` |
| ChEBI<->MeSH crosswalk | Combines two independent methods — a CAS Registry Number bridge (ChEBI's own CAS cross-references joined against MeSH's `RR` fields) and Biomappings' expert-curated exact matches — since neither alone covers most of what's needed. See "ChEBI<->MeSH crosswalk" section below for the full methodology and coverage numbers. Built and validated against real data (2026-07-27); not yet run against production. | `spokebio/ingest/chebi_mesh_crosswalk.py` |
| `PRODUCES` edges (Pathway -> Compound) | Resolves Reactome's `ChEBI2Reactome.txt` through the crosswalk above. 3,287 edges extracted from real data (1,059 distinct compounds, 1,050 distinct pathways) — the ~66% of ChEBI ids that don't resolve are silently dropped, not an error (see crosswalk coverage numbers below). Creates bare `Compound` nodes on demand under the existing `mesh:` namespace only — never invents a `chebi:`-keyed node. Built and validated (2026-07-27); not yet run against production. | `spokebio/ingest/reactome.py::extract_produces`, `spokebio/pipeline.py::run_reactome_ingest` |

## Design Principles

1. **Node keys are each source's own native id, verbatim — no synthetic namespace
   prefix.** Source ID formats are already mutually distinct and self-identifying
   (GO: `GO:0009611`; Reactome: `R-HSA-164843`), and `source_db` disambiguates
   regardless.
2. **Writes touching a `Paper` vertex go through `arcadedb_http.run_script`** (SQL over
   HTTP), never a Cypher/Bolt `SET`/`MERGE` — this avoids the ArcadeDB 26.7.1
   vector-index commit bug on any already-embedded `Paper` node. Node types with no
   `Paper` interaction (e.g. standalone `Pathway` nodes) can use plain Cypher/Bolt
   `MERGE`.
3. **Bookkeeping nodes, not `Paper` properties, for idempotent bulk jobs.**
   `PubtatorChecked` tracks "already processed" as its own node type rather than a
   property on the entity being processed, keeping bulk-job state off vertices that
   carry a vector index.
4. **Each external source is staged and fully validated before the next is added.**
   Sources are integrated one at a time against real downloaded data rather than in
   batch. This document proposes Reactome as the next source, not full parity with
   SPOKE's 41 sources.
5. **Bootstrap nodes on demand, but only under an already-established namespace.**
   `PARTICIPATES_IN`/`PRODUCES` ingestion `MERGE`-creates `Gene`/`Compound` nodes that
   don't exist yet (most of Reactome's referenced entities won't, since MENTIONS only
   creates one when literature happens to mention it) — but only ever keyed
   `ncbigene:<id>` or `mesh:<id>`, the same namespaces PubTator3 already uses. Never
   invent a second namespace (e.g. a raw `chebi:<id>` node) for the same conceptual
   entity type just because a new source's native id doesn't resolve — that risks
   silent duplicate/disconnected nodes for the same real-world gene or compound. This
   is exactly why `PRODUCES` needed the ChEBI<->MeSH crosswalk before writing a single
   edge, rather than keying unresolved compounds by their raw ChEBI id.

## Reactome (Human Pathways) — Implemented

Reactome's `download-data` page returns HTTP 403 to automated fetches; the file layout
below was instead confirmed directly against the public directory
`https://reactome.org/download/current/` (HTTP 200, no license or API key required),
current as of 2026-07-24.

| File | Columns (confirmed) | Use |
|---|---|---|
| `ReactomePathways.txt` | `pathway_id`, `name`, `species` | `Pathway` nodes — filter `species == "Homo sapiens"` (2,883 rows) |
| `NCBI2Reactome.txt` | `ncbi_gene_id`, `pathway_id`, `url`, `pathway_name`, `evidence_code`, `species` | `PARTICIPATES_IN` (Gene -> Pathway). No crosswalk needed — joins directly on the NCBI Gene ID already present in every `Gene` node. |
| `NCBI2Reactome_All_Levels.txt` | Same columns | Same mapping, but includes every ancestor pathway in Reactome's hierarchy per gene rather than only the most specific one (TP53/7157: 47 rows in the base file vs. 131 here). |
| `ChEBI2Reactome.txt` | ChEBI compound id, pathway id, url, pathway_name, evidence_code, species | `PRODUCES` (Pathway -> Compound), resolved through the ChEBI<->MeSH crosswalk below. Implemented. |
| `Pathways2GoTerms_human.txt` | Reactome pathway id, GO id, ... | Optional bridge linking a Reactome `Pathway` node to the GO `Pathway` node already ingested for the same biological concept. |
| `Reactome2OMIM.txt` | pathway/gene id, OMIM id | Human disease linkage — relevant to a future `Disease` node; out of scope for Phase 1. |
| `HumanDiseasePathways.txt` | — | Curated human-disease-specific pathway subset, for narrower scoping if the full 2,883-pathway set proves too broad. |

Evidence codes are GO-style (e.g. `TAS` = Traceable Author Statement); the tiered-trust
filtering approach from `docs/plant_schema.md`'s quality-assessment section applies
directly, with no new framework needed.

### Decisions

1. **Base file vs. `_All_Levels` for `PARTICIPATES_IN` — decided: base file.** Tighter,
   more specific pathway membership; `_All_Levels` adds every broad ancestor pathway
   (e.g. "Apoptosis", "Hemostasis") to every gene beneath it, producing a denser and
   noisier graph around common high-level pathways. Revisit if a query requires
   ancestor-level pathway membership.
2. **ChEBI <-> MeSH crosswalk for `PRODUCES` — built.** See the dedicated section
   below for the full methodology, coverage numbers, and what didn't work.
3. **Scope of SPOKE's broader vocabulary to adopt** (`Disease`, `Protein`, `Anatomy`,
   `SideEffect`, `PharmacologicClass`, ...) — still open. SPOKE's source list
   (`DisGeNET`, `DrugBank`, `UniProt`, `Uberon`, `SIDER`, ...) is a useful reference,
   but per the staging principle above, only Reactome has been proposed/built so far.

## ChEBI<->MeSH Crosswalk

Needed because `PRODUCES` requires resolving Reactome's ChEBI-keyed compound
references back to the `mesh:<id>` namespace already used by every `Compound` node
(from PubTator3). Three approaches were checked; two made it into the final crosswalk.

**Confirmed ruled out**: [TogoID](https://togoid.dbcls.jp), an ID-conversion service
purpose-built for exactly this kind of cross-database bridging. Its own API says so
directly: `GET api.togoid.dbcls.jp/convert?ids=CHEBI:15422&route=chebi,mesh` returns
`{"message":"no route: chebi <> mesh"}` — no path between these two vocabularies
exists in their system at all, for any number of hops.

**Method 1 — CAS Registry Number bridge.** ChEBI's own `database_accession.tsv.gz`
cross-references CAS numbers per compound; MeSH's descriptor/supplementary-concept
files (`d<year>.bin`/`c<year>.bin`) list CAS numbers in their `RR` field. Join on the
CAS number. **Gotcha hit while building this**: a MeSH record's `UI` (its own id)
comes *last* in the record, after its `RR` lines — associating `RR` values with a
"current UI" tracked while scanning forward returns zero matches silently (nothing
errors) instead of the real values, since the UI hasn't been seen yet when each `RR`
line is processed. Fixed by buffering `RR` values per-record and resolving them only
once `UI` is reached.

**Method 2 — [Biomappings](https://github.com/biopragmatics/biomappings).**
Community-curated, expert-reviewed (`semapv:ManualMappingCuration`, `skos:exactMatch`)
mappings across many ontology pairs, published as SSSOM-format TSVs. Includes
3,479 direct ChEBI<->MeSH mappings.

**Coverage, confirmed against real data** (of the 3,223 distinct ChEBI ids Reactome's
human pathways actually reference):

| Method | Coverage |
|---|---|
| CAS bridge alone | 27.9% (898) |
| Biomappings alone | 15.1% (486) |
| **Union (what's built)** | **33.7%** (1,086 in the final module; 1,087 in ad-hoc validation — a 1-item, ~0.1% discrepancy from minor ambiguous-case handling, not investigated further) |

Where the two methods overlap (297 ChEBI ids), they **agree 296/297 times** — a strong
cross-validation signal that both are largely correct rather than coincidentally
similar. The one disagreement (`CHEBI:48416`) is resolved in favor of Biomappings
(expert curation over a mechanical CAS-number match) — the crosswalk's general
tie-break rule.

**Why 33.7% and not higher, and why that's expected, not a failure**: checked
separately (reverse direction), the graph's *existing* 5,071 `Compound` (MeSH) nodes
resolve to a ChEBI id 50.3% of the time — notably higher, since PubTator3-mined
compounds tend to be well-known literature-discussed substances, more likely to have
rich cross-references, unlike many of Reactome's narrower biochemical intermediates.
Checked more broadly still: of *all* of MeSH's actual chemical-type entries (334,542
total — the Chemicals/Drugs descriptor branch plus all Supplementary Concept Records),
only 3.1% resolve to any ChEBI id at all. MeSH (a literature-indexing vocabulary,
comprehensive for "things people write papers about") and ChEBI (a structural
chemistry ontology, comprehensive for "every distinct chemical entity") are
differently-shaped vocabularies with a real but inherently partial intersection — low
overlap isn't a crosswalk defect, it reflects what these two sources actually are.

**Considered, not built**: an LLM-based matching pass over the currently-unresolved
subset (candidate generation via name/synonym overlap, then LLM adjudication of
narrowed candidates, verified against the 296 known-correct agreements as a
calibration check). Plausible follow-up, not attempted — the naive all-pairs version
(218K ChEBI x 334K MeSH) is intractable, and the bounded version needs its own
verification pass given LLM hallucination risk, which wasn't built without a specific
need for the coverage it might add.

**ChEBI's own scope, for context**: 218,253 total compounds, of which only 62,092
(28.4%) are 3-star (fully manually reviewed); 153,251 are 2-star, 2,904 are 1-star.
**ChEBI itself is not among SPOKE's published data sources** (confirmed directly
against `spoke.ucsf.edu/data-tools` — SPOKE's compound-side sources are ChEMBL,
DrugBank, DrugCentral, PharmacoDB, FooDB, and BindingDB). ChEBI only enters this graph
because Reactome — a real, confirmed SPOKE source — happens to use ChEBI internally as
its own chemical-identifier system for pathway participants. Practical implication:
there's no case for "comprehensive ChEBI ingestion" as an independent goal here; the
crosswalk only needs to resolve what Reactome specifically references, not chase
parity with ChEBI's full 218K-compound scope. If SPOKE-alignment calls for a deeper
compound-side source later, ChEMBL or DrugBank are the more natural next candidates —
not ChEBI itself.

## Code Location

`src/spokebio/` (models, schema_ext, upsert, pipeline,
`ingest/{pubtator,go,gene_crosswalk,reactome,chebi_mesh_crosswalk}.py`)
depends on `litgraph`'s core (DB client, config, Paper ingestion, embeddings) as a
library and writes into the same ArcadeDB instance. Not yet wired into `litgraph`'s
main Typer CLI (`cli.py`) — currently run as standalone scripts
(`scripts/pubtator_mentions.py`, `scripts/go_pathways.py`, `scripts/reactome_pathways.py`),
consistent with the staged integration approach above.
