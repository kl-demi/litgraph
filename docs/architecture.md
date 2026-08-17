# Architecture

LitGraph is a literature knowledge graph that links papers from arXiv/Kaggle/PubMed 
with biology entities (genes, compounds, pathways), such that a biology-related query
can draw evidence from academic research.

There are two Python packages that make up the graph:

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
apps/                  Streamlit UI: dashboard.py
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
| Cached figures | `search/stats.py` (`GraphStats` singleton) | §11 |

## 2. Storage backends

`GRAPH_BACKEND` in `.env` selects `arcadedb` (default, deployed) or `neo4j`. One ArcadeDB
server hosts multiple databases; `ARCADEDB_DATABASE` selects one.

Two transports exist for ArcadeDB, deliberately:

- **SQL over HTTP** (`arcadedb_http.py`) — all writes. Cypher/Bolt writes measured
  ~100× slower, and have a history of vector-index bugs on Paper vertices.
- **Cypher over Bolt** (`neo4j_client.py`) — reads, checkpoints, GraphStats updates, and
  everything on the Neo4j backend.

There are different protocols for reads vs. writes because Cypher reads can run against both Neo4j and ArcadeDB backends, while writes, vector search, and full-text search are backend-specific.

## 3. Schema registry

`db/registry.py` defines generic `NodeType`/`EdgeType` shapes. Concrete types are
declared on top of them in schema files — core paper types in `db/schema.py`, biology
types in `spokebio/schema_ext.py` — and passed to `register()`, which drives the DDL.

Conventions enforced by the registry:

- **Node keys are external identifiers, never synthetic ids** — namespaced where two
  sources could collide (`arxiv:2101.00001`, `ncbigene:7157`, `mesh:D009422`), verbatim
  where the source's format is already self-identifying (`GO:0009611`, `R-HSA-164843`).
- `Prop(indexed=True)` is what creates a (non-unique) index; only the key is unique.
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
| Disease | `disease_id` (`mesh:<id>`) | name, doid (indexed) | schema_ext.py |
| Pathway | `pathway_id` (`GO:`/`R-HSA-` verbatim) | name, source_db | schema_ext.py |
| ExtractionChecked | `check_id` (`<extractor>:<paper_id>`) | extractor, paper_id — per-extractor "already checked" bookkeeping | schema_ext.py |

`IngestState` (checkpoint bookkeeping, §6) is created implicitly by MERGE and is not in
the registry. A `Trait` node type and `Gene.locus_id` secondary key exist on the `rice`
branch, alongside the loaders that populate them (§7) — not on `main`.

### Edge types

| Edge | From → To | Properties | Written by |
|---|---|---|---|
| CITES | Paper → Paper | — | enrichment |
| IN_CATEGORY | Paper → Category | — | paper ingest |
| AUTHORED | Author → Paper | — | paper ingest |
| MENTIONS | Paper → Gene/Compound/Organism/Disease | source (extractor) | extraction |
| IS_A | Disease → Disease | — | Disease Ontology |

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

`source` is a `Source` StrEnum (`arxiv`/`kaggle`/`pubmed`/`pubmed_baseline`).

### Category

A `Category` is a subject tag attached to a Paper. For example:

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

`spokebio/models.py` has six types:

- `EntityMention` — one normalized Gene/Compound/Organism/Disease mention
- `Pathway` — carries `source_db` to distinguish Gene Ontology (GO) from Reactome
- `ParticipatesIn` / `Produces` — the edge payloads
- `DiseaseXref` — a Disease Ontology term, keyed by the MeSH id it cross-references
- `DiseaseIsA` — one Disease → Disease subtype claim

The edge payloads carry a GO-style `evidence_code`, which says how the annotation was
made: `TAS` (Traceable Author Statement) means a curator asserted it directly from a
publication; `IEA` (Inferred from Electronic Annotation) means it was assigned by an
automated pipeline, unreviewed. TAS beats IEA when a pair appears via both.

## 5. Write path

### Generic writer

`graph/writer.py` upserts any registered type. Two required policies:

| Argument | Meaning |
|---|---|
| `create_missing` (`NONE`/`SRC`/`DST`/`BOTH`) | Which edge endpoints get created key-only when missing. |
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

- `upsert_papers` computes GraphStats deltas (new papers, stubs, embeddings, date
  range) inline, via `_is_new` sentinels — so `stats overview` reads a singleton
  instead of scanning the graph.
- Category and author edges are separate top-level statements, not folded into the
  same query, because ArcadeDB's Cypher mishandles MERGE inside FOREACH.
- `set_paper_embeddings` touches only the embedding fields, so a backfill can never
  blank out other properties.

### Gene-name maintenance

Reactome-bootstrapped genes are key-only (Reactome's file has no gene symbols).
`spokebio/upsert.py::backfill_gene_names` fills `name` in, null-only, once PubTator3
mentions the same gene with a symbol via `upsert_mentions` — never overwrites a name
already set.

## 6. Paper ingestion

Each source module normalizes records to `Paper`. Most jobs share one loop
(`ingest/pipeline.py::_consume`): batch → embed → upsert. Failed embeddings are
backfilled later via `backfill-embeddings`.

| Source | Key | Categories | Resumability |
|---|---|---|---|
| arXiv API | `arxiv:<id>` | arXiv taxonomy | forward checkpoint |
| Kaggle snapshot | `arxiv:<id>` | arXiv taxonomy | none — full-file stream |
| PubMed E-utilities | `pmid:<id>` | MeSH major topics | forward (daily) or backward (backload); ~9,500-record offset cap |
| PubMed baseline | `pmid:<id>` | MeSH major topics | none |

**Checkpointing** (`ingest/checkpoint.py`): one `IngestState` node per job, storing a
date.

- Daily jobs checkpoint **forward** — advance to the newest date seen each batch.
- The PubMed API backload checkpoints **backward** — walks newest-to-oldest, only
  advancing once a full date-window is ingested, keyed per query string.

**Enrichment** (`enrich`) is a fourth pattern: adds Semantic Scholar citation counts to
existing papers, and creates stub `Paper` nodes for cited papers not yet ingested.

| | Paper datasets | Bio datasets |
|---|---|---|
| Grain | one record = one node | one row = one **edge** |
| Identity | source-assigned, stable | source-local; needs a crosswalk |
| Growth | append-only | full replacement per release |
| Failure mode | a missed paper (recoverable) | a wrong edge (silent, chained) |

This asymmetry is why the two halves share schema/write/query layers but not an
ingestion framework.

## 7. Biology ingestion

| Source | Yields | Key handling |
|---|---|---|
| Gene Ontology (`go-basic.obo`) | Pathway nodes (biological_process) | `GO:` verbatim |
| Reactome | human Pathways, PARTICIPATES_IN, PRODUCES | `R-HSA-` verbatim; genes/compounds via crosswalk |
| NCBI `gene_info` | crosswalk substrate | — |
| ChEBI + MeSH + Biomappings | compound crosswalk | — |
| Disease Ontology (`doid.obo`) | Disease names + `doid`, IS_A edges | MeSH xref → `mesh:` key |

Load order matters: GO's Pathway nodes must exist before Reactome writes edges to 
them. Similarly, PubTator's Disease nodes must exist before Disease Ontology enrich them.

All source downloads share a retry-with-cache helper, reporting rows dropped as duplicates or unresolved if there are any.

## 8. Identity resolution

- **Gene** — Reactome uses NCBI's Entrez Gene id (`ncbigene:<id>`, the `Gene` key). But
  NCBI's own gene file is indexed by **LocusTag**, a separate id scheme, so a crosswalk 
  from LocusTag to Entrez Gene id is needed.
- **Compound** — Reactome uses **ChEBI** ids (a chemistry ontology). But `litgraph`'s
  `Compound` nodes are keyed by **MeSH** id, since PubTator3 (§9) normalizes
  chemicals to MeSH. A ChEBI↔MeSH crosswalk bridges the two.
- **Disease** — Disease Ontology is keyed by **DOID**, but `Disease` nodes are keyed by
  **MeSH** id for the same reason as Compound: PubTator3 normalizes diseases to MeSH.
  DO's `xref` field supplies the mapping, so DOID rides along as an indexed property
  rather than becoming the key.

The `IS_A` hierarchy from Disease Ontology is projected onto MeSH-keyed Disease nodes.
A DO term whose parent carries no MeSH xref is walked further up to its *nearest* mapped
ancestors.

## 9. Entity extraction

Extractors implement a shared `Extractor` protocol (`spokebio/extract.py`): a `name`, 
which Paper properties a candidate needs, and an `extract()` iterator 
yielding `EntityMention`s.


Call `run_extraction()` to run the extraction loop.

- **Coverage** is tracked per extractor (`ExtractionChecked`), so a second extractor
  sees the whole corpus as unchecked.
- **Attribution** — `MENTIONS.source` is set once, on creation; the first extractor to
  find a mention keeps credit for it.

One extractor exists today: `PubTatorExtractor`, wrapping PubTator3's API for
Gene/Compound/Organism/Disease mentions. Disease nodes arrive name-only from a mention
and are enriched afterwards by the Disease Ontology loader (§7-8).

## 10. Query layer

- `search/keyword.py` — full-text index
- `search/semantic.py` — vector index (SPECTER2)
- `search/citations.py` — citation traversals, most-cited
- `search/stats.py` — graph counts and the cached figures behind them (§11)
- `search/entities.py` — name/identifier lookup across whatever entity types a
  database has, discovered from its schema rather than listed in code
- `search/genes.py`, `papers.py`, `pathways.py`, `traits.py`, `compounds.py`,
  `organisms.py` — per-type record + neighbour lookups
- `search/entity_detail.py` — the same, for a type with no module of its own
- `search/corpus.py` — per-database search suggestions, ranked from the graph

Two front ends: the `litgraph` CLI, and `apps/dashboard.py` (Streamlit — a landing page
of corpus figures, search over papers and entities, a page per entity type, and a
SQL/Cypher console that renders results as a graph).

## 11. GraphStats

`GraphStats {id: 'singleton'}` stores cached statistics for quick display:

- **Counters** — papers, stubs, enriched, embedded, authors, categories, the three edge
  totals, and the published date range. Every write in `graph/upsert.py` updates these
  automatically.
- **`edge_endpoints`** — which node types that each edge type joins.
- **`search_hints`** — the suggestion chips under the search box.

Run the rebuild with `litgraph stats rebuild`, or automate it with a cron job.

## 12. Pending work

1. **Housekeeping**

- Resume daily ingestion (enrich + PubTator + fetch) on the AWS box' crontab. ***Note***: arXiv jobs must run during US daytime to not get blocked (arXiv goes on maintenance at night).

2. **Data ingestion**

- Expand beyond Reactome/GAF for pathway edges (the SPOKE-style curated datasets: DrugBank, STRING, disease ontologies, etc.).

- Close the gap with SPOKE (see last section of `spoke_schema.md`).

3. **LLM extraction / query layer**

- Scope a minimal NL &rarr; Cypher wrapper: can start with a fixed set of query templates (gene &rarr; pathway, author &rarr; papers) before open-ended generation, to avoid Cypher hallucination.

- Scope out literature-backed edge-chain extraction, once there's more pathway coverage.

4. **Frontend**

- Common-search menu (top authors, most-mentioned genes, etc.); we already have the components for this in `search` module.
- Better graph visualization.

5. **Infra**

- Keep in mind the ArcadeDB OOM checklist and the ~505s index rebuild after restarts.
