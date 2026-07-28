# Known Bugs

Serious bugs found during LitGraph development: what broke, why, and how it was
fixed. Day-to-day debugging detail belongs in commit messages — this log is for
issues worth a lasting record (silent data loss, upstream engine bugs, anything
that could recur).

## Open

### `backload-pubmed-api` can't ingest past ~10,000 papers per query
**Found:** 2026-07-28 (building the rice corpus) | **Component:** `ingest/pubmed_source.py::_esearch_with_history` / `_efetch_history_batch`

`_esearch_with_history`'s docstring claims `usehistory=y` "sidesteps esearch's own
10,000-result retmax cap" so efetch "can then page through the whole set via retstart".
That is false: the ceiling applies to efetch history paging too. Probed directly against
E-utilities on a 51,166-record set — `retstart=9000` returns 200, `retstart=9999`,
`10000` and `20000` all return **400**, with and without `sort=pub_date`. (The boundary
is slightly ragged: `retstart=9950&retmax=100` succeeded, `retstart=9999&retmax=1` did
not, so it isn't a clean `retstart+retmax` rule.)

400 isn't in `_is_retryable` (only 429 and 5xx), so the run dies with an unhandled
`HTTPStatusError`. The rice backload stopped at exactly **9,800** papers of 51,166 —
`batch_size=200`, so the first request at `retstart=10000` crashed it.

**Consequence:** no single MeSH query can yield more than ~9,800 papers, silently
capping every PubMed corpus. `lg2`'s PubMed side is likely subject to the same limit.

**Fix direction:** slice the query into date windows (e.g. per year via `mindate`/
`maxdate`) so each esearch set stays under 10,000, and page within each window. Retrying
the 400 won't help — the record is genuinely unreachable by that offset.

### `backload-pubmed-api --limit` can loop forever without ingesting anything new
**Found:** 2026-07-28 (building the rice corpus) | **Component:** `ingest/pipeline.py::run_backload_pubmed_api`

Resume uses a **date** checkpoint (min `published_date` seen), but progress inside a run
is an efetch `retstart` **offset**. When `--limit` stops a run before the walk crosses
the boundary date, the checkpoint is rewritten to the same date it started from, so the
next invocation re-issues an identical query and re-upserts the identical records.

Observed on rice: four consecutive `--limit 5000` chunks each reported "5000 papers
upserted, batch spans 2025-01-01 to 2026-12-31" while `Paper` count stayed pinned at
5,000 and the checkpoint stayed at `2025-01-01`. MERGE makes the re-writes idempotent, so
there is no data corruption — it just spins, hammering NCBI, and the per-run log looks
successful throughout.

Aggravated by the same future-`PubDate` skew as the resolved daily-fetch bug below:
`maxdate` filters on Entrez's `pdat` index while the checkpoint is computed from the
parsed XML `PubDate`, so records dated 2026-12-31 come back under `maxdate=2025/01/01`
and the min parsed date is a poor proxy for how far the walk actually got.

**Workaround:** omit `--limit` (one unbounded run per query) — though that then hits the
~10,000 ceiling above. **Fix direction:** checkpoint the offset within a date window
rather than a bare date, which the date-window slicing above would give for free.

### ArcadeDB semantic search degrades past ~230K vectors
**Found:** 2026-07-22 | **Component:** `search/semantic.py`, ArcadeDB `LSM_VECTOR` index

`vector.neighbors` query latency is stable (sub-2s) up to ~150-190K embedded papers,
then degrades sharply — occasional 5-15s stalls through ~230K, then sustained
multi-minute unresponsiveness on both reads and writes past ~230-250K. Reproduced on
a clean local instance with real production embeddings, on both server versions
26.7.1 and 26.7.2, so it's a genuine ArcadeDB engine bug (GC thrashing), not a
litgraph query or heap-sizing issue — heap usage at breakdown was only 60-70%.

**Status:** unresolved. ArcadeDB's next release, 26.8.1, is targeted for ~2026-08-03
and may address it — check its release notes before re-testing or filing an upstream
issue.

## Resolved

### `enrich` crashed mid-run on a short Semantic Scholar batch response
**Found:** 2026-07-27 (production, via `cron_daily_ingest.sh`) | **Component:** `ingest/semantic_scholar.py::_enrich_batch`

S2's `/paper/batch` endpoint can return fewer entries than ids requested — silently
omitting one instead of returning a `null` placeholder in its slot. `_enrich_batch`
matched responses back to requests by position (`zip(pairs, items, strict=True)`),
so a short response raised `ValueError: zip() argument 2 is shorter than argument 1`.
Because `cron_daily_ingest.sh` runs under `set -e`, this killed the whole daily
pipeline run, not just the enrich step — PubTator extraction never ran that day.

**Fix:** match each pair back to its response item by the returned `externalIds`
value instead of array position; any id with no matching item (dropped, or a
genuine `null`) is treated as not-found, same as before.

### PubMed daily fetch silently stuck for a week on a future checkpoint date
**Found:** 2026-07-27 | **Component:** `ingest/pipeline.py::run_daily_fetch_pubmed`

A journal issue's `PubDate` can be dated months ahead of its actual indexing date.
One paper with a `PubDate` of December 2026 pushed the fetch checkpoint into the
future; every subsequent run then queried an inverted date range (`mindate` >
`maxdate`), which always returns zero results. The job "succeeded" with 0 new papers
every day for a week with no error.

**Fix:** the checkpoint now only advances using `published_date <= today`. Checkpoint
was manually reset to the last true good date.

### ArcadeDB 26.7.1: any write touching an embedded Paper failed at commit
**Found:** 2026-07-16 | **Component:** ArcadeDB server (26.7.1), `LSM_VECTOR` index

Any transaction that touched a `Paper` vertex already tracked by the
`Paper[embedding]` vector index — inserting an embedding, or just updating an
unrelated field on an already-embedded paper — failed deterministically with
`IllegalStateException: Timer already cancelled`. Reproduced over both Bolt and SQL,
so it was a genuine server bug, not specific to litgraph's Bolt driver usage. Hit
`enrich`, `fetch-daily`, and `backfill-embeddings` simultaneously.

**Fix:** upgraded ArcadeDB to 26.7.2, which hardened the LSM index against this case.
Confirmed still running 26.7.2 as of 2026-07-27.

### RunPod embedding outage silently produced a mostly-unembedded corpus
**Found:** 2026-07-15 | **Component:** `ingest/pipeline.py::_embed_remote`

The RunPod GPU embedding endpoint had no retry logic — a single request failure
crashed the whole multi-hour arXiv backload. After adding retries, a second failure
mode surfaced: a sustained outage (pod down, not transient) meant retries exhausted
and papers were correctly upserted without an embedding rather than losing the run —
but nothing flagged that ~108K of ~110K backloaded papers ended up unembedded.

**Fix:** added tenacity retry (6 attempts / 3 min) around the embed call, and a new
`litgraph backfill-embeddings` command to re-embed any paper missing one after the
fact. Operationally: check embedded-paper count against total-paper count after any
large ingestion run rather than assuming a "successful" run means fully embedded.

### `enrich` permanently stuck reprocessing the same papers
**Found:** 2026-07-15 | **Component:** `ingest/pipeline.py::run_enrichment`

Papers Semantic Scholar reported as "not found" never got `enriched_at` stamped, so
`enrich` re-selected the same ~25 not-found papers on every run instead of ever
reaching new ones — a real correctness bug, not just wasted API calls.

**Fix:** not-found papers are now stamped as enriched too, so the cursor moves past
them.

### `upsert_paper_stubs` silently dropped `pmid` from citation stubs
**Found:** 2026-07-15 | **Component:** `graph/upsert.py::upsert_paper_stubs`

A missing field in the stub-write parameters meant citation stub nodes for PubMed
papers were created without their `pmid`, breaking later lookups/merges keyed on it.

**Fix:** added the missing field to the upsert parameters.

### arXiv `fetch-daily` 429s traced to cron timing, not a code regression
**Found:** 2026-07-22, root-caused 2026-07-27 | **Component:** cron schedule, not litgraph code

`fetch-daily` hit a hard 429 from `export.arxiv.org` on the very first request of the
run, for 6 consecutive days. Initially looked like a transient rate-limit blip. It
wasn't: the cron job was scheduled at `0 3 * * *` UTC — 23:00 US-Eastern, inside
arXiv's nightly maintenance/high-load window — while manual runs at other hours
always succeeded.

**Fix:** rescheduled the cron entry to `7 14 * * *` UTC (mid-morning Eastern). No
retry/backoff logic was touched, since the failure was a scheduling problem, not a
transient one. **Lesson:** before adding retry logic to a recurring-failure bug,
check whether the failure correlates with time-of-day before assuming it's the
request itself.
