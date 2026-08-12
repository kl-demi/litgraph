# Timeline

How each component evolved, oldest first within each section. `docs/architecture.md`
describes only the current state; this file is where "used to be" lives.

## Project direction

- **2026-07-07** — started as `arxiv_graphdb`: a Neo4j graph of arXiv papers.
- **2026-07-09** — migrated to ArcadeDB; renamed to `litgraph`.
- **2026-07-10** — PubMed added as a second paper source.
- **2026-07-21** — biology layer begun as `plantbio` (Arabidopsis/plant focus, see
  `docs/plant_schema.md`).
- **2026-07-24** — pivoted from plant schema to a human/SPOKE-adjacent schema; `plantbio`
  renamed to `spokebio` (see `docs/spoke_schema.md`). Plant schema kept as historical
  record.
- **2026-08-03** — rice corpus added as a third database: full 51K-paper Oryza corpus,
  Trait Ontology + Oryzabase + gene gazetteer. Rice uses GAF (not Reactome) for pathway
  edges.
- **2026-08-07** — the rice-specific loaders (GAF, Oryzabase, gene gazetteer, Trait
  Ontology, gene-name/locus-id backfill, their eval scripts) moved off `main` onto a
  `rice` branch (09c1983): they only serve one corpus, while `main` stays the
  source-agnostic core plus GO/Reactome. Took the `Trait` node type, `ASSOCIATED_WITH`
  edge type, and `Gene.locus_id` index with them; `gene_crosswalk.py` reverted to its
  pre-rice generic `gene_info` crosswalk (`build_locus_tag_crosswalk` only). The
  registry-driven writer, Category/Source models, and PubMed date-window fix that had
  landed alongside them on the same branch were kept on `main`.

## Schema

- **2026-07-21** — biology types created in `plantbio/schema_ext.py`, ArcadeDB-only
  (raised `NotImplementedError` for Neo4j) while the core `db/schema.py` supported both
  backends. Adding a node type required four coordinated edits (model, vertex-type list,
  unique-key list, upsert function).
- **2026-08-05** — `Gene.locus_id` added as a NOTUNIQUE secondary key (103 rice locus ids
  map to more than one NCBI gene).
- **2026-08-06** — declarative registry (`db/registry.py`): one `NodeType`/`EdgeType`
  declaration per type, DDL for both backends generated from it. `schema.py` and
  `schema_ext.py` became declarations into the shared registry; the Neo4j gap closed.
  One deliberate rename: the Neo4j index `paper_s2_id` became the generated
  `paper_s2_paper_id`.
- **2026-08-07** — `NodeType.bootstrappable` added; `upsert_edges` enforces it.

## Paper identity

- Originally three optional fields (`arxiv_id`, `pmid`, `s2_paper_id`) with an if-chain
  for `Paper.id`, duplicated in `CitationStub`. arXiv ids were stored bare
  (`2101.00001`) while PubMed/S2 were prefixed (`pmid:`, `s2:`).
- **2026-08-06** — replaced with `Paper.identifiers: dict` + `PAPER_IDENTIFIERS`
  (011636d). arXiv ids initially stayed bare to match live data; then prefixed uniformly
  (`arxiv:2101.00001`), which collapsed `IdentifierNamespace`'s separate `name`/`prefix`
  fields into one. Requires `scripts/migrate_paper_ids.py` on live databases (unrun).

## Category / MeSH

- Originally `Category.code` held arXiv taxonomy codes and MeSH descriptor *names* in one
  global unique index, with nothing but string shape keeping the vocabularies apart. All
  MeSH headings were ingested, so a PubMed paper carried 3–6× an arXiv paper's
  categories, mostly NLM check tags (Humans, Male, Adult...).
- **2026-08-06** — codes namespaced (`arxiv:cs.CL`, `mesh:D009422`), MeSH re-keyed on the
  descriptor UI (the parser had been reading the UI attribute and discarding it);
  `vocabulary` and `name` added as properties; `source` became a StrEnum. MeSH filtered
  to major topics with an all-headings fallback for papers flagging none major.
  Requires `scripts/migrate_category_keys.py` on live databases (unrun); major-topic
  filtering cannot be applied retroactively (`MajorTopicYN` was never stored).

## Write path

- **2026-07-09..14** — writes were Cypher over Bolt; per-edge hand-written queries.
- **2026-07-14..21** — hot paths (edge counts, stubs, citation edges) rewritten from
  Cypher to SQL over HTTP after measuring the ~100× gap and hitting the ArcadeDB 26.7.1
  vector-index commit bug on Paper writes.
- **2026-07-21..08-03** — biology upserts accumulated as seven per-type queries in
  `spokebio/upsert.py` and `graph/upsert.py`, with the bootstrap/update policies stated
  only in comments.
- **2026-08-07** — generalized into `graph/writer.py` (`upsert_nodes`/`upsert_edges`,
  registry-driven, policies as required arguments). All ArcadeDB writes now via
  SQL/HTTP. `upsert_papers` kept bespoke for its GraphStats delta accounting (524f440).

## Paper ingestion

- **2026-07-07..10** — arXiv API + Kaggle snapshot backload; then PubMed via baseline
  files and E-utilities; SPECTER upgraded to SPECTER2.
- **2026-07-13** — embedding moved to a RunPod GPU server; retries and
  `backfill-embeddings` added after an outage left ~108K papers unembedded.
- **2026-07-14** — Semantic Scholar enrichment (citation counts, stubs, CITES edges).
- **2026-07-24..28** — PubMed API backload hardened: date-based checkpoint, then split
  into date windows after hitting the ~9,500 efetch offset ceiling.
- **2026-08-06** — sources emit namespaced `Category` objects and `Source` enum values;
  PubMed keeps only major-topic headings; the baseline `--mesh-terms` filter follows.
- **2026-08-07** — `arxiv_source.py` and `pubmed_source.py` carried byte-identical
  `IngestState` checkpoint get/set code (differing only in a default job name); hoisted
  into `ingest/checkpoint.py`, with the job name now chosen by the caller in
  `pipeline.py` instead of defaulted per source. The five `run_*` jobs' batch-embed-upsert
  loop (accumulate → embed → upsert → track earliest/latest) was the same code repeated
  four times with minor variations (a `date_filter` for PubMed's future-PubDate guard);
  collapsed into one `_consume` helper. The fifth, `run_backload_pubmed_api`, kept its own
  loop — its per-date-window checkpoint advancement doesn't reduce to the same shape.

## Biology ingestion

- **2026-07-21** — PubTator3 MENTIONS ingestion (Gene/Compound/Organism), with
  `PubtatorChecked` bookkeeping. Compound keyed `mesh:` because PubTator normalizes
  chemicals to MeSH, not ChEBI.
- **2026-07-24** — GO biological_process terms as Pathway nodes; NCBI `gene_info`
  crosswalk; Reactome human pathways + PARTICIPATES_IN.
- **2026-07-27** — PRODUCES edges via the ChEBI↔MeSH crosswalk (CAS bridge +
  Biomappings, 33.7% coverage); GO/Reactome release-check script.
- **2026-07-28** — GAF loader for non-human (rice) pathway edges.
- **2026-08-03** — Trait Ontology nodes + Oryzabase ASSOCIATED_WITH edges; rice gene
  gazetteer for MENTIONS (PubTator finds rice genes 4.9% of the time); eval worksheets.
- **2026-08-05** — gene naming layers: gene_info names, Oryzabase CGSNL symbols with MSU
  fallback, locus-id backfill.
- **2026-08-07** — after the rice split left `main` with four `ensure_<x>_file`
  downloaders (go.py, reactome.py, gene_crosswalk.py, plus chebi_mesh_crosswalk.py's
  three, which already shared one internal `_download`), hoisted the retry-with-cache
  body into `spokebio/ingest/_download.py::ensure_cached_file`; each source now supplies
  only its own URL and path. Also gave `reactome.py`'s `extract_participates_in`/
  `extract_produces` the same `(edges, rows_considered, dropped_*)` NamedTuple shape the
  removed GAF/Oryzabase loaders used — previously Reactome's drop rates (species filter,
  evidence-tie dedup, ChEBI resolution failures) were mentioned only in docstring
  percentages, not counted at runtime.
- **2026-08-09** — first dashboard cut (`apps/dashboard.py`, Streamlit): Overview counts
  (via a new registry-driven `stats.type_counts()`), keyword/semantic search, and
  citation/gene neighborhoods drawn as graphviz node-edge graphs. Verified against lg2;
  its keyword search surfaced that lg2's `Paper[title,abstract]` full-text index no
  longer exists — moot, since lg2 is being replaced by a fresh database.
- **2026-08-09** — extractor interface (`spokebio/extract.py`): `run_pubtator_mentions`'s
  candidate-select/flush/mark-checked loop was PubTator-specific — the `rice` branch's
  gazetteer had to duplicate it. Now `Extractor` (name, requires, extract) + a generic
  `run_extraction` loop; `PubtatorChecked` became per-extractor `ExtractionChecked`
  (keyed `<extractor>:<paper_id>`) so a second extractor tracks its own coverage;
  `MENTIONS.source` became required, making first-writer-wins the enforced conflict rule.
  The fresh-database plan is what made the bookkeeping rename free (no migration).

## ArcadeDB server

- **2026-07-16** — 26.7.1 → 26.7.2: any write touching an embedded Paper failed at
  commit (`Timer already cancelled`).
- **2026-08-03** — 26.7.3: hotfix for a data-loss regression in super-node edge-append
  merges (rice's taxon 4530 alone carries ~41K MENTIONS in-edges).
- **2026-08-06** — 26.8.1: fixed semantic search silently returning `[]` on lg2 — index
  tombstones scored `Infinity` similarity and crowded every live vector out of the
  top-K. lg2 had accumulated tombstones over 569 enrich runs; rice, written once, was
  unaffected. Originally misdiagnosed as GC thrashing past ~230K vectors.
- **2026-08-07** — rebuild-on-restart stall (~505s on lg2's 291K vectors) mitigated by
  `arcadedb-vector-warmup.service`, a oneshot unit that absorbs the first-query rebuild
  after every restart (504s → 1.3s measured). Details in `docs/known_bugs.md`.

- **2026-08-11/12** — Disease added as an entity type. PubTator's Disease annotations
  are kept as nodes (MeSH-keyed, like Compound), and a Disease Ontology loader enriches
  them with DO's name and `doid` plus an `IS_A` hierarchy projected onto MeSH keys.
  `check_pathway_releases.py` reports the DO release alongside GO and Reactome.
- **2026-08-12** — the Streamlit dashboard was rebuilt and merged from `frontend`:
  search-first UI, entity pages for every connected type plus a schema-driven fallback,
  a SQL/Cypher console rendering results as graphs, and per-database search hints read
  from the live schema. `apps/gene_explainer.py` was deleted — the dashboard supersedes
  it. Design notes are kept out of the repo (`docs/frontend.*` is gitignored).

## Documentation

- `docs/plant_schema.md` — superseded 2026-07-24, kept as historical record.
- `docs/spoke_schema.md` — the SPOKE-adjacent schema and per-source status.
- `docs/architecture.md` — written 2026-08-05 as a source-agnostic vs. source-dependent
  analysis; rewritten 2026-08-07 as a current-state guide, with history moved here; kept
  in sync same-day as the rice split and the checkpoint/loop/`ensure_file`/drop-accounting
  consolidations landed.
- **2026-08-09** — `scripts/migrate_category_keys.py`/`migrate_paper_ids.py` dropped from
  pending work: rather than keep fixing `lg2`'s write-performance pathology in place, the
  plan is a fresh database, so nothing needs migrating. §12's TODOs replaced with two next
  steps: LLM-based entity/relationship extraction from paper text, and a dashboard app
  (`search/genes.py` and `apps/gene_explainer.py` were early groundwork for the latter;
  the dashboard landed 2026-08-12 and the explainer was removed).
