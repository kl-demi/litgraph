# Architecture

LitGraph is a literature knowledge graph, containing papers ingested from arXiv/Kaggle/PubMed, plus
biology entities (genes, compounds, pathways) extracted from those papers or loaded from
curated databases, such that a query can traverse from a Paper to the biology it
is evidence for. Two Python packages share one database:

- `src/litgraph/` — the source-agnostic core: schema registry, models, write path, search,
  paper ingestion, CLI.
- `src/spokebio/` — the biology extension: bio node/edge types, bio source loaders, entity
  extraction. Depends on `litgraph` as a library.

Development history lives in `docs/timeline.md`; serious bugs in `docs/known_bugs.md`.

```
src/litgraph/
  config.py            settings (.env): backend choice, credentials, API keys
  models.py            Paper, CitationStub, EnrichmentResult, Category, Source
  db/registry.py       declarative schema registry + DDL emitters
  db/schema.py         core paper types, declared into the registry
  db/arcadedb_http.py  SQL / sqlscript over HTTP (default write path)
  db/neo4j_client.py   Cypher over Bolt (reads, Neo4j backend)
  graph/writer.py      generic registry-driven upsert_nodes / upsert_edges
  graph/upsert.py      paper-specific writes + GraphStats accounting
  ingest/              per-source paper fetchers + run_*_ingest jobs
  search/              keyword, semantic, citations, stats, genes (bio)
  cli.py               `litgraph` Typer app
src/spokebio/
  models.py            EntityMention, Pathway, ParticipatesIn, Produces
  schema_ext.py        bio types, declared into the same registry
  extract.py           extractor protocol + generic extraction loop
  upsert.py            policy choices over graph/writer + gene-name backfill
  ingest/              bio source loaders (GO, Reactome), crosswalks, extractors
  pipeline.py          run_*_ingest jobs
apps/                  Streamlit UIs: dashboard.py (main), gene_explainer.py (demo)
scripts/               operational entry points and one-off migrations
data/                  cached source downloads (gitignored)
```

## 1. Layer map

| Layer | Files | Details |
|---|---|---|
| Backend adapters | `db/arcadedb_http.py`, `db/neo4j_client.py`, `config.py`, `run_log.py` | §2 |
| Schema | `db/registry.py`, `db/schema.py`, `spokebio/schema_ext.py` | §3 |
| Models | `litgraph/models.py`, `spokebio/models.py` | §4 |
| Write path | `graph/writer.py`, `graph/upsert.py`, `spokebio/upsert.py` | §5 |
| Paper ingestion | `litgraph/ingest/*` | §6 |
| Bio ingestion | `spokebio/ingest/*`, `spokebio/pipeline.py` | §7 |
| Identity resolution | `spokebio/ingest/{gene,chebi_mesh}_crosswalk.py` | §8 |
| Entity extraction | `spokebio/extract.py`, `spokebio/ingest/pubtator.py` | §9 |
| Query | `litgraph/search/*`, `cli.py` | §10 |

## 2. Storage backends

`GRAPH_BACKEND` in `.env` selects `arcadedb` (default, deployed) or `neo4j`. One ArcadeDB
server hosts multiple databases; `ARCADEDB_DATABASE` selects one.

Two transports exist for ArcadeDB and the split is deliberate:

- **SQL / sqlscript over HTTP** (`arcadedb_http.py`) — all writes. Batched Cypher MERGEs
  over Bolt measured ~100× slower, and Cypher writes touching embedded Paper vertices have
  a history of hitting vector-index commit bugs (see known_bugs).
- **Cypher over Bolt** (`neo4j_client.py`) — reads, checkpoints, GraphStats updates, and
  everything on the Neo4j backend. ArcadeDB's Bolt plugin is a reimplementation with
  quirks; the client retries its spurious `TransactionNotFound`.

## 3. Schema registry

`db/registry.py` defines generic nodes and edges. Specific types are declared on top of these generic shapes inside schema files, where they are passed to `register()` to run the DDL.

There are separate schema files for core paper types (`litgraph/db/schema.py`) and biology types (`spokebio.schema_ext.py`). 

Conventions enforced by the registry:

- **Node keys are external identifiers, never synthetic ids** — namespaced where two
  sources could collide (`arxiv:2101.00001`, `ncbigene:7157`, `mesh:D009422`), verbatim
  where the source's format is already self-identifying (`GO:0009611`, `R-HSA-164843`).
- `Prop(indexed=True)` creates a (non-unique) index; only the key is unique.
- `NodeType.bootstrappable` declares whether an edge write may create the node key-only
  when absent (§5). True only for Gene, Compound, Organism.
- `validate()` rejects an edge whose endpoint type isn't registered.

### Node types

| Node | Key | Other properties | Declared in |
|---|---|---|---|
| Paper | `id` (`arxiv:`/`pmid:`/`s2:`) | identifier columns, title, abstract, is_stub, enriched_at, embedding (vector), fulltext(title, abstract) | schema.py |
| Category | `code` (`arxiv:cs.CL`, `mesh:D009422`) | vocabulary, name | schema.py |
| Author | `name` | — | schema.py |
| GraphStats | `id` (singleton) | counters, undeclared | schema.py |
| Organism | `taxon_id` (bare NCBI Taxonomy) | name | schema_ext.py |
| Gene | `gene_id` (`ncbigene:<id>`) | name | schema_ext.py |
| Compound | `compound_id` (`mesh:<id>`) | name | schema_ext.py |
| Pathway | `pathway_id` (`GO:`/`R-HSA-` verbatim) | name, source_db | schema_ext.py |
| ExtractionChecked | `check_id` (`<extractor>:<paper_id>`) | extractor, paper_id — per-extractor "already checked" bookkeeping | schema_ext.py |

`IngestState` (checkpoint bookkeeping, §7) is created implicitly by MERGE and is not in
the registry. A `Trait` node type and `Gene.locus_id` secondary key exist on the `rice`
branch, alongside the loaders that populate them (§8) — not on `main`.

### Edge types

| Edge | From → To | Properties | Written by |
|---|---|---|---|
| CITES | Paper → Paper | — | enrichment |
| IN_CATEGORY | Paper → Category | — | paper ingest |
| AUTHORED | Author → Paper | — | paper ingest |
| MENTIONS | Paper → Gene/Compound/Organism | source (extractor) | extraction |
| PARTICIPATES_IN | Gene → Pathway | evidence_code | Reactome |
| PRODUCES | Pathway → Compound | evidence_code | Reactome |

An `ASSOCIATED_WITH` (Gene → Trait) edge type exists on the `rice` branch, not `main`.

## 4. Models

### Paper identity

A paper can carry a different identifier depending on where it came from — an arXiv id,
a PMID, a Semantic Scholar id. `Paper.identifiers` centralizes all of these into one
dict of namespace → id, e.g. `{"arxiv": "2101.00001"}`, instead of a separate optional
field per source:

- `PAPER_IDENTIFIERS` is the one list a new paper source gets added to; it validates
  the dict's keys.
- `Paper.id` (the MERGE key) is the highest-preference identifier present, prefixed by
  its namespace: `arxiv:2101.00001`, `pmid:12345678`, `s2:<id>`.
- Flat constructor kwargs like `Paper(arxiv_id=...)` fold into the dict.

Identifiers are modelled as a dict, but **stored as flat columns**
(`arxiv_id`, `pmid`, `s2_paper_id`):

- search and pipeline queries read them as plain properties
- they need range indexes
- ArcadeDB can't index into a map field

The columns and indexes are generated from `PAPER_IDENTIFIERS` too, so adding a source
is still a one-place edit.

`source` is a `Source` StrEnum (`arxiv`/`kaggle`/`pubmed`/`pubmed_baseline`).

### Category

A `Category` is a subject tag attached to a Paper — arXiv's own topic taxonomy for
arXiv papers, MeSH headings for PubMed papers. One node type holds both vocabularies;
namespacing the key keeps them apart:

- **arXiv** — key is `arxiv:cs.CL`. The part after the prefix is arXiv's own taxonomy
  code, already human-readable.
- **MeSH** — key is `mesh:D009422`. The part after the prefix is the descriptor's
  **UI**, an opaque id, not the human-readable name. The readable
  display name (e.g. "Neoplasms") is stored separately, in a `name` property.

A PubMed paper's MeSH headings are filtered before they become Categories:

- Most headings are incidental (population metadata like Humans, Male, Adult), so only
  ones flagged as major topics are kept (`MajorTopicYN="Y"`) — the indexer's own
  statement of what the paper is actually about.
- **Fallback:** if no heading on a paper is flagged major, all of its headings are kept
  instead, so it isn't left with zero categories.

### Bio models

`spokebio/models.py` has four types:

- `EntityMention` — one normalized Gene/Compound/Organism mention
- `Pathway` — carries `source_db` to distinguish GO from Reactome
- `ParticipatesIn` / `Produces` — the edge payloads

The edge payloads carry a GO-style `evidence_code`, which says how the annotation was
made: `TAS` (Traceable Author Statement) means a curator asserted it directly from a
publication; `IEA` (Inferred from Electronic Annotation) means it was assigned by an
automated pipeline, unreviewed. TAS beats IEA when a pair appears via both.

## 5. Write path

### Generic writer

`graph/writer.py` upserts any registered type. Callers name a type and pass plain-dictionary
rows; keys and edge endpoint types come from the registry. Two required policies:

| Argument | Meaning |
|---|---|
| `create_missing` (`NONE`/`SRC`/`DST`/`BOTH`) | Which absent edge endpoints get a key-only INSERT. Rows with other absent endpoints are dropped. |
| `update_existing` (bool) | Rewrite properties on match. False when another job may have written better values; true when this loader is the authority. |

Bootstrap eligibility is declared once, on the type, via `NodeType.bootstrappable`:

- **Eligible** — ids are validated upstream (by a crosswalk or NER normalization)
  before reaching a writer, and a key-only node is already complete, its other
  properties being optional enrichment. Gene, Compound, and Organism qualify.
- **Not eligible** — an ontology term like Pathway doesn't: the graph lookup is its
  only id validation, and some terms are marked obsolete, which can't be filled in
  after the fact.

`upsert_edges` raises if a call site tries to bootstrap a non-bootstrappable type.

Policy per edge, as chosen in `spokebio/upsert.py` and `graph/upsert.py`:

| Edge | create_missing | update_existing | Note |
|---|---|---|---|
| PARTICIPATES_IN | SRC (Gene) | true | Pathway must pre-exist |
| PRODUCES | DST (Compound) | true | crosswalk already resolved the mesh: id |
| MENTIONS | NONE | false | nodes written in a prior pass; first extractor keeps `source` |
| CITES | NONE | false | stubs pass runs first, with title + identifiers |

### Paper-specific writes

`graph/upsert.py` keeps custom queries where the generic writer has no equivalent:

- `upsert_papers` computes GraphStats deltas (new papers, upgraded stubs, embedding
  delta, date range) inside the write itself, via `_is_new` sentinels, so counters stay
  correct even under re-ingestion.
- Category and author edges are written as separate top-level statements, not folded
  into the same query, because ArcadeDB's Cypher mishandles MERGE inside FOREACH and
  nested list params.
- `set_paper_embeddings` touches only the embedding fields, so a backfill can never
  blank out other properties.

`GraphStats` itself is a singleton the counters accumulate into, so `stats overview`
never has to scan the graph.

### Gene-name maintenance

Reactome-bootstrapped genes are key-only (Reactome's file has no gene symbols).
`spokebio/upsert.py::backfill_gene_names` fills `name` in, null-only, once PubTator3
mentions the same gene with a symbol via `upsert_mentions` — never overwrites a name
already set.

## 6. Paper ingestion

Each source module normalizes to `Paper`. Most jobs share one loop,
`ingest/pipeline.py`'s `_consume` helper: a source hands it an iterator of `Paper`, and
it batches, embeds, and upserts them, returning `(total, earliest, latest)`. If
embedding fails, papers are upserted unembedded and `backfill-embeddings` re-embeds them
later.

| Source | Transport | Format | Key | Categories | Resumability |
|---|---|---|---|---|---|
| arXiv API | `arxiv` lib | Atom results | `arxiv:<id>`, version stripped | arXiv taxonomy | forward checkpoint, newest-first |
| Kaggle snapshot | local file | JSON-lines | `arxiv:<id>` | arXiv taxonomy | none — full-file stream + filters |
| PubMed E-utilities | esearch/efetch | PubmedArticle XML | `pmid:<id>` | MeSH major topics | forward checkpoint (daily) or backward checkpoint (API backload); ~9,500 offset ceiling |
| PubMed baseline | local bulk files | `pubmed*.xml.gz` | `pmid:<id>` | MeSH major topics | none |

### Checkpointing

`ingest/checkpoint.py` provides two generic primitives — `get_checkpoint(job)` and
`set_checkpoint(date, job)` — that read or write one date under a job name, backed by an
`IngestState` node. The primitives have no notion of "forward" or "backward" on their
own; that comes entirely from how a caller uses them:

- **Forward** — the two daily-fetch jobs (arXiv, PubMed) use `_consume()`, which
  advances the checkpoint to the newest `published_date` seen after each batch. This is
  "papers since I last saw."
- **Backward** — `run_backload_pubmed_api` does not use `_consume()`; it has its own
  loop. It walks PubMed newest-to-oldest, and only advances the checkpoint — to the
  oldest date reached — once a full date-window has been fully ingested. It's keyed per
  query string (`pubmed_backload_api:<mesh_terms>`), so a different `--mesh-terms` value
  starts its own walk. This is "how far into history have I gotten."

The job name is always chosen by the caller in `pipeline.py`, never defaulted inside a
source module, so there's exactly one place that decides what a job is called.

### A fourth pattern: enrichment

Semantic Scholar (`enrich`) doesn't fit the fetch-and-upsert shape above — it enriches
Papers already in the graph with citation counts, and creates **stub** Paper nodes
(`is_stub=true`, filled in if later ingested) for citation endpoints it doesn't have yet.
Stubs are matched back to real papers by `externalIds`, since S2's batch endpoint
silently omits ids it doesn't recognize.

### Paper vs. bio datasets

| Dimension | Paper datasets | Bio datasets |
|---|---|---|
| Grain | one record = one node | one row = one **edge**; nodes come from ontology files |
| Identity | source-assigned, stable | source-local; crosswalk required before any write |
| Growth | append-only, checkpointable by date | full replacement per release |
| Load ordering | none | strict: crosswalk → ontology nodes → annotation edges |
| Unknown endpoints | create a stub, fill later | drop and count; never mint a new namespace |
| Failure mode | a missed paper, recoverable | a wrong edge — plausible, silent, chained onward |
| Provenance | `source` on Paper | `source_db` on node, `evidence_code` on edge, `source` on MENTIONS |

This asymmetry is why the two halves share schema/write/query layers but not an ingestion
framework.

## 7. Biology ingestion

Two sources feed the graph:

| Source | What it yields | Format | Native key handling |
|---|---|---|---|
| GO (`go-basic.obo`) | Pathway nodes (biological_process, non-obsolete) | OBO 1.2 | `GO:` verbatim |
| Reactome (`download/current/*.txt`) | human Pathways, PARTICIPATES_IN, PRODUCES | headerless TSV | `R-HSA-` verbatim; genes `ncbigene:`-prefixed; ChEBI via crosswalk |
| NCBI `gene_info` | crosswalk substrate only (`build_locus_tag_crosswalk`) | `#`-header TSV, gzip | — |
| ChEBI + MeSH + Biomappings | ChEBI→MeSH compound crosswalk | TSV / ASCII records / SSSOM | — |

Load order matters: GO's Pathway nodes must exist before Reactome's edges are written.
`run_go_ingest` runs first; `upsert_participates_in`/`upsert_produces` `MATCH` the
Pathway rather than create it, so an edge pointing at a term GO hasn't written yet is
simply dropped.

A few conventions shared across these loaders:

- **Downloading** — all four source files (GO, Reactome, `gene_info`, and the three
  ChEBI/MeSH/Biomappings files) go through one retry-with-cache helper,
  `ensure_cached_file`. Each source just supplies its own URL and destination path.
- **MeSH parsing** — a MeSH record's `UI` field comes after the fields that reference
  it, so the parser has to buffer a whole record before it can key it. MeSH also has no
  "current" alias to fetch, so `DEFAULT_MESH_YEAR` is bumped by hand each year.
- **Evidence conflicts** — when Reactome reports two evidence codes for the same
  (gene, pathway) or (compound, pathway) pair, the more trusted one wins (`TAS` over
  `IEA`, see §4). `extract_participates_in`/`extract_produces` also count how many rows
  were considered, dropped as duplicates, or dropped as unresolved (e.g. no ChEBI→MeSH
  match) — logged on every run, so a rising drop rate shows up as a number instead of
  quietly getting worse.

## 8. Identity resolution

Reactome's bulk files name genes and compounds using identifier schemes that don't match
`litgraph`'s own node keys. A crosswalk resolves one to the other before anything is
written: *identifier string → canonical namespaced key, or nothing — never invent a key*.

- **Gene** — Reactome refers to genes by NCBI's Entrez Gene id, a stable numeric id NCBI
  assigns per gene; this is the `ncbigene:<id>` namespace `Gene` nodes are keyed on. But
  NCBI's own `gene_info` file isn't indexed by that id directly — it's indexed by
  **LocusTag**, a systematic identifier a genome-annotation project assigns to a gene,
  independent of any human-readable symbol. `gene_crosswalk.py::build_locus_tag_crosswalk`
  maps `gene_info`'s LocusTag column to `ncbigene:<id>`. Rows with no LocusTag are
  skipped, never keyed some other way.
- **Compound** — Reactome refers to small molecules by **ChEBI** id (`CHEBI:<id>` —
  Chemical Entities of Biological Interest, a chemistry-focused ontology). But
  `litgraph`'s `Compound` nodes are keyed `mesh:<id>` instead, since PubTator3 (§10)
  normalizes chemical mentions to MeSH, not ChEBI. `chebi_mesh_crosswalk.py` bridges the
  two via two independent methods — a CAS-number bridge and Biomappings curation — with
  33.7% combined coverage of Reactome's referenced ids. Unresolved ids are dropped
  rather than keyed under a second namespace.

## 9. Entity extraction

Extractors implement a shared `Extractor` protocol (`spokebio/extract.py`), instead of
each writing its own fetch/checkpoint/upsert loop:

- `name` — a stable string identifying the extractor
- `requires` — which Paper properties a candidate must have (e.g. `pmid` for an API
  keyed on PMIDs)
- `extract()` — an iterator turning candidate papers into normalized `EntityMention`s

`run_extraction()` is the shared loop that drives any extractor: it selects papers the
extractor hasn't checked yet, upserts mentions in batches, then marks every candidate
checked — including papers the extractor found nothing for, so they don't reappear in
the next run.

Two things are tracked per extractor, not globally:

- **Coverage** — `ExtractionChecked` is keyed `<extractor>:<paper_id>`, so a second
  extractor sees the whole corpus as unchecked, regardless of what the first one covered.
- **Attribution** — `MENTIONS.source` records which extractor created an edge. It's set
  once, on creation, and never updated — so if two extractors find the same
  (paper, entity) mention, the first one to write it keeps the attribution.

One extractor exists today: `PubTatorExtractor` (`name="pubtator3"`,
`requires=("pmid",)`), which wraps `PubTatorClient`'s batched, rate-limited calls to
PubTator3's API and yields Gene/Compound/Organism mentions.

## 10. Query layer

Query modules, each wrapping one kind of read:

- `search/keyword.py` — full-text index
- `search/semantic.py` — SPECTER2 vector index
- `search/citations.py` — CITES traversals, most-cited
- `search/stats.py` — GraphStats overview + rebuild, plus per-type node/edge counts
- `search/genes.py` — gene lookup, papers mentioning a gene, pathways, co-mentioned
  genes; the first bio query surface

Two front ends sit on top of these modules:

- the `litgraph` CLI — `search keyword|semantic`, `citations`, `stats
  count|latest|oldest|most-cited|top-authors|overview|rebuild`, `runs`
- `apps/dashboard.py`, a Streamlit UI (`streamlit run apps/dashboard.py`) with Overview,
  Papers, Citations, and Biology pages; citation and gene results are drawn as
  node-and-edge graphs

Bio queries beyond genes still run as ad-hoc SQL, outside this layer.


## 11. Pending work

Next steps, in priority order:

1. Dashboard app: a prettier, task-specific alternative to ArcadeDB Studio. A first cut
   exists (`apps/dashboard.py`, `streamlit run`): Overview (GraphStats + registry-driven
   per-type counts), Papers (keyword/semantic search), Citations and Biology pages that
   draw query results as node-and-edge graphs. Remaining: paper-centric entity view,
   pathway/compound explorers, and wiring in the ingestion-status side (`litgraph runs`).
2. LLM extraction of entities and relationships directly from paper text, beyond what
   PubTator3's NER and Reactome's curated files currently yield.

   This would also close a gap in how evidence is modeled today: `Paper -MENTIONS->
   Gene` is **literature-backed** (derived from text), but `Gene -PARTICIPATES_IN->
   Pathway` and `Pathway -PRODUCES-> Compound` are **ontology-backed** (from Reactome's
   curated files, no paper attached). A query that traverses Paper → Gene → Pathway
   can't tell "this paper says gene X participates in pathway Y" apart from "this paper
   mentions gene X, and Reactome separately says X participates in Y." Open design
   question: attach the evidence as a location within the paper, or as the text span
   itself.
3. `log_run()` and release-version stamping for bio ingest jobs (§8).
4. A bridge from ontology-asserted edges to a supporting publication as an alternative to
   #2 — GAF's `DB:Reference` column is one candidate, once a GO-annotation-file source
   returns to `main`.
5. Drug-drug interaction from a pharmacological dataset (unscoped).
