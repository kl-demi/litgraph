# Plant biology node schema (draft)

**Superseded 2026-07-24** — litgraph's direction pivoted to a human/SPOKE-focused
schema instead; see `docs/spoke_schema.md`. Kept here as historical record of the
plant-biology exploration (Arabidopsis/PlantCyc/Magnoliaceae) — the `plantbio` code
package itself was renamed to `spokebio` and repurposed, not extended along this path.

Extends litgraph's existing `Paper` / `Author` / `Category` vertex types (see
`src/litgraph/db/schema.py`) with a set of biological entity types. Each new type
follows the same pattern already used there: a Pydantic model in `models.py`, an
entry in `_ARCADEDB_VERTEX_TYPES`, a unique index on its natural key, and a
MERGE-based upsert function in `graph/upsert.py`.

Design rule for every node below: the **key** is an ID from an existing external
ontology/database, not a synthetic litgraph ID. That's what lets this graph
cross-reference AgroLD, PubChem, OREGANO, and Planteome instead of drifting into
its own private vocabulary.

## Node types

| Node | Key (unique index) | Core properties | Backing ontology / DB | Notes |
|---|---|---|---|---|
| **Organism** | `taxon_id` (NCBI Taxonomy) | `scientific_name`, `common_name`, `rank` | NCBI Taxonomy | Anchors every other node to a species. One row per species you cover — starts as a single row (e.g. Arabidopsis thaliana) but the type stays generic so adding a second species later is a data problem, not a schema change. |
| **Gene** | `gene_id` (namespaced, e.g. `TAIR:AT1G01010`) | `symbol`, `synonyms[]`, `description`, `organism` (ref) | Species gene DB (TAIR, RAP-DB, MaizeGDB, ...), cross-ref Entrez Gene | PubTator3 already tags gene mentions in text, normalized to NCBI Gene ID — reuse that instead of building gene NER from scratch. |
| **Protein** | `uniprot_id` | `name`, `encoded_by` (ref to Gene), `organism` (ref) | UniProt | Optional split from Gene — see "open decisions" below. Only add this node if you need protein-level data (interactions, structure) that doesn't belong on the gene. |
| **Compound** | `chebi_id` (fallback `pubchem_cid`) | `name`, `synonyms[]`, `formula`, `compound_class` (e.g. "phytohormone", "flavonoid") | ChEBI / PubChem | PubTator3's "chemical" tag is usually MeSH/ChEBI-normalized already — same reuse logic as Gene. |
| **Pathway** | `pathway_id` (MetaCyc/PlantCyc ID, or GO ID for broader biological_process terms) | `name`, `description`, `source_db` | MetaCyc / PlantCyc, GO (biological_process branch) | The mechanistic layer connecting Gene/Protein/Compound — this is what actually "explains" a phenotype. |
| **Anatomy** | `po_id` | `name`, `category` (`structure` \| `growth_stage`), `synonyms[]` | Plant Ontology (PO) | Where/when something happens — organ, tissue, cell type, developmental stage. |
| **Trait** | `to_id` | `name`, `description` | Trait Ontology (TO) | The *named dimension* being measured (e.g. "leaf size"), not the observed value. |
| **Condition** | `peco_id` | `name`, `category` (`abiotic_stress` \| `biotic_stress` \| `treatment` \| ...) | Plant Experimental Conditions Ontology (PECO) | The experimental context a phenotype was observed under (drought, pathogen exposure, a specific treatment). |
| **Phenotype** | see "open decisions" | `description` (from source text), `pato_id` (quality), refs to `Anatomy`/`Trait` | PATO (quality ontology) + Anatomy/Trait composition | The trickiest node — an *observed instance*, not a stable external entity. See below before committing to this as a first-class node. |

Existing and unchanged: **Paper** (`id` = arxiv_id / pmid / s2_paper_id), **Author** (`name`),
**Category** (`code` — currently arXiv taxonomy; PubMed papers could instead/also carry
MeSH headings here).

**Implemented and live** (`src/plantbio/`, ahead of this doc in a few respects): `Organism`
(`taxon_id`, bare NCBI Taxonomy id), `Gene` (`gene_id`, namespaced `ncbigene:<id>`),
`Compound` (`compound_id`, namespaced `mesh:<id>` — PubTator3 normalizes chemicals to
MeSH, not ChEBI, despite the table above assuming `chebi_id`; a real ChEBI/PubChem
crosswalk is still open work), all populated via `MENTIONS` edges from `Paper`, built by
`plantbio/ingest/pubtator.py` + `plantbio/pipeline.py`. `PubtatorChecked` is a bookkeeping
node (not a domain entity) tracking which papers have already been queried.

Also implemented and live: `Pathway` nodes for GO's `biological_process` branch, built by
`plantbio/ingest/go.py` + `plantbio/pipeline.py::run_go_ingest` — 24,129 non-obsolete
`biological_process` terms ingested as of 2026-07-23, keyed on GO's own native id verbatim
(e.g. `GO:0009611`, `source_db="GO"`; see "Pathway design" below for why no extra prefix
is needed). **Nodes only** — no `PARTICIPATES_IN`/`HAS_PATHWAY`/`PRODUCES` edges have been
written, so the Gene ID crosswalk blocker described below doesn't block what's live today.
PlantCyc/MetaCyc (the species-specific half) is not yet built: blocked on submitting
plantcyc.org's license agreement and downloading PGDB files.

## Pathway design: species-agnostic and species-specific in one node type

The instinct to want both a species-agnostic and a species-specific pathway concept is
right, but it doesn't need two node types. The BioCyc family (MetaCyc/PlantCyc and every
per-species PGDB like AraCyc) already solves this in its own architecture: a pathway
*frame ID* is reused as-is across the reference database and every organism's PGDB that
has evidence for it — the pathway's identity is inherently species-agnostic, and
"species-specific" just means *which genes, in which organism, have been linked to that
same ID*. GO biological_process terms work the same way: the term ID is universal, and
species-specificity lives entirely in which genes are annotated to it. So:

**One `Pathway` node**, keyed on `pathway_id` using each source's own native id
verbatim — no synthetic namespace prefix needed, since GO's and MetaCyc's id formats
are already mutually distinct and self-identifying (confirmed live: GO ids always look
like `GO:0009611`; MetaCyc/PlantCyc frame ids never do), and `source_db` is an explicit
disambiguator anyway:
- MetaCyc/PlantCyc frame id as-is (e.g. `PWY-101`) — covers PlantCyc's pan-species
  experimentally-verified set and any single-species PGDB pathway, since they share the
  same frame id when present in both
- GO id as-is (e.g. `GO:0009611`) for Gene Ontology biological_process terms —
  **implemented and live**, see above

Properties: `name`, `source_db` (`MetaCyc` \| `PlantCyc` \| `<Species>Cyc` \| `GO`),
`description`.

Species-agnostic vs. species-specific is then entirely a property of the **edges**, not
the node. **None of the following edges are built yet** — today's GO ingestion is
nodes-only (see above); this is still design, not implementation status:
- `(:Gene)-[:PARTICIPATES_IN {role, evidence_type}]->(:Pathway)` — inherently
  species-specific, since `Gene` nodes are already species-scoped (an `ncbigene:<id>` is
  one organism's gene, never shared across species).
- `(:Organism)-[:HAS_PATHWAY {source_pgdb, evidence_type}]->(:Pathway)` — an
  organism-level marker, useful even before gene-level detail is parsed, and gives a
  direct "which pathways does species X have" query without traversing through genes.
- `(:Pathway)-[:PRODUCES]->(:Compound)` — reuses the `Compound` nodes already in the
  graph from the PubTator pipeline.

`evidence_type` on both edges (`experimental` \| `computational_prediction` for
PlantCyc/PGDB data, or the raw GO evidence code for GO data) carries forward the
tiered-trust rule from earlier in this doc — filterable at query time rather than
baked into two different node types.

### The blocking prerequisite: a Gene ID crosswalk

`Gene.gene_id` today is namespaced `ncbigene:<NCBI Gene ID>` (that's what PubTator3
normalizes to). PlantCyc/PGDB and GO annotation files identify genes primarily by
locus/AGI code (TAIR-style, e.g. `AT1G32640`) or UniProt accession, not NCBI Gene ID
directly. Writing `PARTICIPATES_IN` edges from pathway data without resolving this
first will silently create duplicate `Gene` nodes for the same biological gene under a
different key — exactly the kind of bug that's invisible until you notice the graph
has two disconnected nodes for MYC2.

Two ways to close this, in order of preference:
1. Resolve every pathway-sourced gene reference to the existing `ncbigene:` key at
   ingest time, using a crosswalk file (NCBI's `gene_info`/`gene2accession` for the
   species, or TAIR's own ID-mapping table) before calling the existing Gene upsert.
2. If a clean resolution isn't available for some fraction of genes, add a secondary,
   indexed-but-not-unique property on `Gene` (e.g. `locus_id`) and match on whichever
   identifier the source provides, while keeping `gene_id` (`ncbigene:`) as the one
   canonical unique key so PubTator- and pathway-sourced writes always converge on the
   same node.

Either way, this needs solving *before* the pathway ingestion writes its first edge —
it's not something to patch up after the fact once duplicate nodes exist.

### Ingestion shape is different from the PubTator pipeline

`pubtator-mentions` is literature-driven and incremental — it runs per newly-ingested
`Paper`. Pathway data isn't literature-derived; it's a periodic bulk export from
GO's/PMN's own releases, keyed by gene, not by paper. That makes it closer to
litgraph's original `backload` pattern (a batch job re-run when a new GO/PlantCyc
release ships) than to the per-paper `pubtator-mentions` job — plan for a
`plantbio/ingest/plantcyc.py` and `plantbio/ingest/go.py`, each a one-shot loader
against a downloaded flat-file/export, both writing into the same `Pathway` upsert
functions rather than two different schemas.

**`plantbio/ingest/go.py` is built and live, but only the first half of the
"species-agnostic" bullet below** — it bulk-loads GO's ontology file (`go-basic.obo`)
into bare `Pathway` nodes (term identities only, no evidence, no gene linkage). The
second half — a GOA gene-annotation/GAF file, parsed per-species into
`PARTICIPATES_IN` edges carrying the real evidence code — is separate, not-yet-built
work, still just the design below. `plantbio/ingest/plantcyc.py` doesn't exist yet
(blocked on the license step, see above).

Two loaders for the MVP:
- **Species-specific**: your chosen species' PGDB from PMN (flat-file export) →
  `Pathway` nodes + `PARTICIPATES_IN` edges from that species' genes + `HAS_PATHWAY`
  from its `Organism` node, each edge tagged `experimental` or
  `computational_prediction` per PMN's own curation flag.
- **Species-agnostic**: GO biological_process annotations for the same species (a GOA
  plant annotation file, e.g. TAIR's own) → `Pathway` (GO) nodes + `PARTICIPATES_IN`
  edges from genes, each carrying the real GO evidence code so the earlier
  IEA-vs-curated filtering rule can actually be applied.

Both funnel into the same node/edge shape above — this is two data loaders, not two
schemas.

**Optional, not MVP:** `(:Pathway)-[:SUB_PATHWAY_OF]->(:Pathway)` to capture MetaCyc's
own super-/sub-pathway hierarchy, if you find yourself wanting pathway-of-pathways
queries later.

**Operational note, same as the MENTIONS pipeline:** these loaders should follow the
same additive-only discipline already established — no unconditional `SET` on existing
`Gene`/`Organism`/`Paper` vertices (risk of clobbering fields written by the live
AWS enrichment job), match-then-create-if-missing for every node and edge, safe to run
as a distinct batch job against the same ArcadeDB instance.

## Open decisions

**Gene vs. Protein — merge or split?** For an MVP, merging them into a single `Gene`
node (with a `uniprot_id` field alongside the locus id) is simpler and avoids an
edge type (`ENCODES`) you may not query often. Split them only once you have data
that's genuinely protein-level (interaction networks, structural data) and doesn't
belong on the gene record.

**Phenotype: node or edge property?** Two options:
- *As a node* (above): lets you attach a single phenotype to multiple papers as
  evidence, and query "all phenotypes observed for this trait" independent of
  what caused them. Costs more — you'll mint a lot of near-duplicate Phenotype
  nodes since most are one-off observations from a single paper.
  As a synthetic key, use a tuple hash of (organism, anatomy_id or trait_id, pato_id)
  so repeated observations of the *same* phenotype statement collapse into one node.
- *As an edge property instead*: drop the standalone node, and put `pato_quality`,
  `direction` (increased/decreased/abolished), and an evidence reference directly
  on the edge connecting the cause (Gene/Compound/Condition) to the Trait/Anatomy
  it affects. Simpler graph, but you lose the ability to treat "this phenotype"
  as a queryable thing independent of any one causal edge.

  Recommendation: start with the edge-property version for MVP simplicity: e.g.
  `(:Gene)-[:AFFECTS {quality: pato_id, direction: "increase", evidence_paper: pmid}]->(:Trait)`.
  Promote to a first-class Phenotype node later if you find yourself needing to
  aggregate multi-paper evidence for the same observed phenotype.

**Pathway scope.** Some pathway IDs (MetaCyc/PlantCyc) are species-specific
instances; GO biological_process terms are species-agnostic. Decide per-pathway
which you're pulling in — PlantCyc for the specific species you're focused on,
GO for cross-species process-level claims extracted by the LLM layer.

## Suggested build order

1. **Phase 1 (minimum viable graph):** `Organism`, `Gene`, `Compound`, `Pathway` —
   plus the already-existing `Paper`/`Author`/`Category`. Enough to represent
   "gene X is part of pathway Y, pathway Y produces compound Z, evidenced by paper W."
   **Status:** `Organism`/`Gene`/`Compound` nodes + `MENTIONS` edges from `Paper` are
   live (PubTator3); `Pathway` nodes are live for GO's `biological_process` branch only.
   Still open: PlantCyc/MetaCyc's species-specific pathways, and every edge that would
   actually connect these nodes together (`PARTICIPATES_IN`, `HAS_PATHWAY`,
   `PRODUCES`) — the graph currently has the right node types but no pathway-level
   traversal yet.
2. **Phase 2:** `Anatomy`, `Trait`, `Condition`, and the `AFFECTS`-style edges that
   carry phenotype claims. This is where the LLM extraction pass over PubMed
   abstracts starts contributing new edges rather than just new nodes.
3. **Phase 3 (optional):** promote `Phenotype` to a standalone node if multi-paper
   evidence aggregation becomes a real need.

## Cross-references to keep in mind

- PubTator3 tags: gene, chemical, species (and disease/variant/cell_line, less
  relevant here) — reuse for `Gene`, `Compound`, `Organism` mentions instead of
  building NER for those from scratch.
- Planteome already curates and cross-links PO, TO, PECO, GO, ChEBI, PATO, and
  NCBI Taxonomy — the ontology set above isn't an arbitrary choice, it's adopting
  what Planteome (and by extension AgroLD) already uses, which is what makes
  cross-referencing those graphs tractable.

## Source integration: costs, and don't do it all at once

Every external source added is a standing liability, not a one-time cost:

| Cost | Why it bites |
|---|---|
| Entity resolution | Each source has its own ID scheme and granularity (TAIR vs. UniProt vs. NCBI Gene vs. MeSH for the same gene). Crosswalks between them are incomplete and occasionally wrong — this is real, ongoing work, not a one-off script. |
| ETL maintenance | AgroLD (RDF/SPARQL), PubChem (bulk API/dumps), Planteome (OBO files), OREGANO (its own graph dump) each ship on their own release cadence and format. Five sources means five things that can silently go stale or break on format changes. |
| Conflicting claims | Different pathway DBs model the same biology at different granularity (MetaCyc vs. KEGG reaction boundaries, for instance). Merging without a conflict policy (keep both with provenance? prefer one source? confidence-score?) produces a graph that quietly contradicts itself. |
| Access friction | SPARQL endpoints can be slow/rate-limited; some bulk downloads are large; licensing/attribution terms vary per source. |
| Overlap / diminishing returns | ChEBI and PubChem both give compound identity; GO and MetaCyc both encode some pathway info. Not all 5 sources are additive — some are redundant for your scope. |

Recommendation: **stage it, don't batch-integrate.** Start with the minimum that
lets you test the actual hypothesis (does LLM-extracted, literature-grounded
pathway data produce useful graph queries at all): the Planteome ontology
backbone (PO/TO/PECO/GO/ChEBI/NCBI Taxonomy) as the ID system, PlantCyc for one
species' pathways, and PubMed literature via litgraph's existing ingestion +
PubTator3 for the gene/chemical/species bootstrap layer. Add AgroLD, PubChem
proper, or OREGANO later, one at a time, only when a specific gap in query
results points at a concrete missing source — each addition treated as its own
scoped project (crosswalk validated before it's trusted), not a "wire up
everything" sprint.

## Where should this code live?

Not inside `litgraph`'s core schema/models. litgraph's actual value is a
general-purpose, source-agnostic literature graph engine (arXiv, Kaggle, PubMed
already) — that's what makes it worth building on in the first place. Hardcoding
nine plant-specific vertex types into `db/schema.py` and `models.py` conflates
that reusable engine with one domain project; anyone (including future-you)
wanting litgraph for an unrelated corpus inherits plant-biology schema baggage
they don't need.

The underlying worry — "steering the graph db into one use case" — isn't really
about ArcadeDB the engine (it can host multiple databases, or multiple
non-overlapping vertex/edge types within one, without conflict); it's about the
Python codebase. So the fix is modularizing code, not standing up a separate
database or service — a fully separate DB would sever the one thing that makes
this worth doing: single-query traversal from a Paper straight to the Gene/
Pathway/Compound it's evidence for.

Concretely: a new subpackage in the same repo, depending on `litgraph`'s core
(DB client, config, CLI scaffolding, Paper ingestion, embeddings) as a library,
but owning its own models/schema/ETL/extraction code and writing into the same
ArcadeDB instance:

```
src/
  litgraph/        # core: Paper/Author/Category, CLI, DB client, embeddings — untouched
  plantbio/        # new: everything in this doc
    models.py      # Organism, Gene, Compound, Pathway, Anatomy, Trait, Condition, ...
    schema_ext.py  # registers new vertex/edge types + indexes on the *same* ArcadeDB instance
    ingest/
      agrold.py
      pubchem.py
      planteome.py   # PO/TO/PECO/GO pulls
      pubtator.py    # bootstrap NER/normalization layer
    extract/
      llm_extract.py # LLM relation extraction over litgraph's ingested Paper abstracts
    cli.py           # new typer sub-app, mounted onto litgraph's existing `app`
```

This is cheap and reversible now (nothing plant-specific exists in litgraph yet,
so there's nothing to untangle later). If litgraph's core ever needs reuse for
an unrelated domain and `plantbio`'s dependencies become a real burden, split it
into its own installable package at that point — premature to do it now.

## Source quality assessment

Not all of these are equally trustworthy out of the box. Roughly four tiers:

**Trust as-is.** NCBI Taxonomy — stable, authoritative, essentially no
controversy about species identity. GO's *structure* (the DAG itself) is solid;
the caveat is entirely about which annotations riding on it you trust (next
point).

**Trust selectively — filter by evidence/curation level.** GO annotations carry
an evidence code, and IEA ("Inferred from Electronic Annotation" — i.e.
automated, uncurated) is by far the most common one. GO annotation quality is
documented to have real annotation bias (most annotations cluster on a few
well-studied genes) and IEA-derived facts are measurably less reliable than
manually curated ones. Practical rule: pull GO terms tagged with experimental or
curated evidence codes (EXP/IDA/IMP/IGI/... ) as higher-confidence, treat
IEA-only annotations as computational leads worth double-checking, not ground
truth.

**Good backbone, but depth varies a lot by species/scope.** PlantCyc's own
single-species databases mix literature-curated data with PathoLogic's
computational predictions that aren't all curator-reviewed — quality is
genuinely uneven: a well-studied model organism gets rich curated pathways, an
obscure species mostly gets computational predictions with "pathway holes."
This is a real reason to pick a well-studied species for your first pass — the
data quality difference is not small. PO and TO's core vocabularies are mature
and stable, but the crop-specific trait extensions (via Crop Ontology, ~35-40
species) are described in the literature as having "limited semantics" —
essentially flat species-specific term lists, not deeply structured ontology —
so don't expect fine-grained trait distinctions for anything but well-resourced
crops.

**Useful, but with acknowledged, specific gaps for plant work.** ChEBI is
excellent generally, but its own literature acknowledges incomplete coverage of
natural products and secondary metabolites specifically — exactly the compound
class plant metabolism is full of (alkaloids, terpenoids, flavonoids). Expect
more "not in ChEBI" misses here than you would for standard drug/human-metabolism
compounds; fall back to PubChem CID when that happens, as already noted above.
PubTator3 posts F1 ≥ 0.9 on its standard entity types, but that's measured
mostly on biomedical/human-centric literature — there's published evidence of a
real domain gap for plant-science text (a general biomedical NER model scored
only ~0.32 macro F1 on plant-specific content in one direct comparison), and
PubTator specifically doesn't pre-annotate plant-specific gene nomenclature
(e.g. Arabidopsis-style locus IDs) well, especially in multi-species documents.
Treat PubTator's species/chemical tagging as a solid bootstrap; treat its gene
tagging on plant literature with real skepticism — that gap is very likely
exactly what your own LLM extraction pass needs to be doing the real work on,
not a redundant double-check.

## Live test: PubTator3 on a real plant paper

Called the real API directly — `GET
https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson?pmids=<PMID>`
— against PMID 17616737 ("MYC2 differentially modulates diverse jasmonate-dependent
functions in Arabidopsis," Plant Cell 2007). Actual results, not a prediction:

**Worked well:**
- `MYC2` and its synonym `JIN1` both correctly resolved to the same NCBI Gene ID
  (840158); `PDF1.2` correctly resolved to its own gene ID (834469).
- `jasmonate` / `JA` / `jasmonic acid` all consistently normalized to the same
  MeSH chemical ID (C011006) across ~10 mentions — abbreviation resolution held up.
- Tryptophan, Ascorbic Acid, Flavonoids all correctly tagged and normalized.
- `Arabidopsis` and `Arabidopsis thaliana` both correctly resolved to NCBI Taxonomy
  3702; `Helicoverpa armigera` (an insect pest mentioned in the same abstract)
  correctly tagged as its own species — multi-species text didn't confuse it here.

**Confirmed gaps, concretely:**
- `indole glucosinolate` — a real, biologically important plant defense compound
  class — came back completely unnormalized (`"valid": false`, no ID). This is
  exactly the secondary-metabolite coverage gap flagged above, not a theoretical
  concern.
- `insect` (from "resistance to insect pests") was mistagged as the *disease*
  "Entomophobia" (MeSH C000719201) — a pure human-clinical-domain artifact with
  no relevance to plant text. Worse, this bad tag cascaded into an extracted
  relation, "jasmonic acid **treats** Entomophobia," at 0.80 confidence — a
  concrete example of a single wrong entity tag producing a fluent-looking but
  nonsense relation. Confidence scores alone won't catch this.
- Genes come back normalized to NCBI Gene IDs (integers), not the plant
  community's native locus codes (MYC2 is `AT1G32640` in TAIR/AGI convention) —
  usable, but needs an NCBI Gene ↔ TAIR crosswalk before it'll join cleanly with
  AgroLD or TAIR-keyed data.

**Verdict, matching the tiered assessment above but now with evidence:** gene,
chemical, and species tagging are a genuinely solid bootstrap layer, even on
plant-specific text. Disease tagging should probably be dropped entirely for
this use case rather than filtered — it's not just weaker, it actively produces
wrong tags on ordinary words. Relations need a real sanity/filter step (e.g.
drop anything touching a Disease entity, keep Gene/Chemical/Species pairs above
a score threshold) rather than being taken as-is. Secondary metabolites will
need the ChEBI-then-PubChem-fallback plan already noted, or LLM extraction to
fill the gap directly.

**Suggested next step:** a small script that, for each `Paper` already ingested
via `backload-pubmed-api`, calls this same endpoint, stores the raw annotations,
applies that filter policy, and writes surviving Gene/Chemical/Species mentions
as `MENTIONS` edges from Paper to the corresponding node — before touching the
LLM extraction layer at all, since this gets you real graph edges for free on
whatever PubTator already handles well.

**Status: done.** See the "Implemented and live" note near the top of this doc —
`plantbio/ingest/pubtator.py` does exactly this, deployed and run against the AWS box.
