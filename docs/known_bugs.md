# Known Bugs

Serious bugs: what broke, why, how it was fixed. Latest first. Day-to-day debugging
belongs in commit messages; this log is for silent data loss, upstream engine bugs, and
anything that could recur.

## Open

### ArcadeDB gets killed by the Linux OOM killer
**Recurring since 2026-07-21** | AWS host, `arcadedb.service`

**Symptom:** ArcadeDB stops responding; `systemctl status arcadedb` shows
`Failed with result 'oom-kill'`; `dmesg -T | grep -i oom` shows a kernel `Out of memory:
Killed process ... (java)` line. This is a **kernel** OOM kill, not a Java
`OutOfMemoryError` — the JVM never got a chance to log one.

**Root cause:** memory over-commit, not an undersized heap. The box (7.6GB, no swap)
runs ArcadeDB (`-Xmx6G`, ~6.6GB resident) alongside other processes on the same host
(the ingest job, ~800MB). Margin is normally thin; whichever process pushes the machine
over 100% triggers the kernel to kill the biggest consumer, which is always the JVM. An
earlier theory (heap undersized) was checked and ruled out — do not re-raise it; `-Xmx`
was already raised from 4G to 6G to fix a real, distinct heap OOM (see the July 21 case
below), and lowering it back to 4G would reintroduce that bug.

**Troubleshoot, in order:**
1. `systemctl status arcadedb` — confirm the service actually came back up. It does not
   auto-restart from an OOM kill.
2. `dmesg -T | grep -i oom` vs. ArcadeDB's own log (`java.lang.OutOfMemoryError`) vs.
   `df -h` (disk full) — these are three different bugs with three different fixes.
3. `free -h` for current RAM/swap.
4. `ps -ef | grep "[j]ava.*ArcadeDBServer" | grep -o "Xmx[0-9A-Za-z]*"` — confirm the
   configured heap. After an instance resize, this does **not** update automatically;
   check it explicitly rather than assuming it matches the new instance size.
5. Check what else was running at kill time (`ps aux` sorted by `rss`, not `total_vm` —
   `snapd`/`tailscaled`-style daemons look GB-scale by `total_vm` but use 15-20MB `rss`).
   Both confirmed triggers so far were the ingest job and an unrelated system process,
   never ArcadeDB's own growth.

**Fix, cheapest first:**
1. Add 2-4GB swap — no downtime, no config change, turns a hard kill into a slowdown.
2. Stop co-scheduling heavy ingestion with the database on the same box.
3. Resize the instance if both genuinely need to co-reside.
4. Drop a database's vector index if its semantic search is broken anyway — the largest
   corpus's 291K-vector index was the single biggest memory consumer on this host.

**Separate July 21 case, now closed:** an earlier incident on the same host *was* a real
Java heap OOM (`-Xmx` was 768MB, undersized for any load) — fixed by raising `-Xmx` to
4G, then to 6G after a subsequent measurement. That fix is done; it is not the same bug
as the kernel OOM kills above, and re-lowering the heap would resurrect it.

**Separate cause of dirty shutdowns, fixed:** `systemctl stop` used the default
90s timeout, and a vector-graph rebuild ignores SIGTERM while running, so systemd
SIGKILLed the JVM mid-build. **Fix:** `TimeoutStopSec=600` in
`/etc/systemd/system/arcadedb.service`.

### ArcadeDB rebuilds the whole vector graph on every restart
**2026-08-06** | ArcadeDB 26.8.1, `LSM_VECTOR` index

The first `vector.neighbors` query after a server restart rebuilds the vector graph from
scratch (~505s on lg2's 291K vectors, ~84s on rice's 51K); subsequent queries are
sub-second. The server persists a `.vecgraph` file but does not reload it on startup.
**Mitigation (2026-08-07):** `arcadedb-vector-warmup.service` runs
`/usr/local/bin/arcadedb-vector-warmup`, firing one `vector.neighbors` per database once
the server is serving. It is `PartOf=arcadedb.service` and pulled in by a `Wants=` there,
so it re-fires on every restart, not only at boot. Measured: the warm-up absorbed 476.3s
(lg2) and 73.9s (rice), and the next query returned in 1.32s instead of 504s. It is a
separate unit rather than `ExecStartPost=` because that would block startup past
`TimeoutStartSec=120` and get the JVM SIGKILLed mid-build. Worth an upstream issue.

### `backload-pubmed-api` can't ingest past ~10,000 papers per query
**2026-07-28** | `ingest/pubmed_source.py`

efetch history paging returns 400 past `retstart≈9,500` regardless of `usehistory=y`,
and 400 isn't retryable, so the run dies — the rice backload stopped at exactly 9,800 of
51,166. Date-window splitting (each window under the cap) is implemented; a single day
exceeding the cap is still yielded truncated and reported, not fixed.

### `backload-pubmed-api --limit` can loop forever without ingesting anything new
**2026-07-28** | `ingest/pipeline.py::run_backload_pubmed_api`

Resume checkpoints a **date**, but in-run progress is an efetch **offset**. A `--limit`
stop before the walk crosses the boundary date rewrites the same checkpoint, so the next
run re-upserts identical records — observed spinning at 5,000 papers across four runs.
Idempotent MERGEs mean no corruption, just wasted NCBI calls that log as success.
**Workaround:** omit `--limit`. **Fix direction:** checkpoint offset-within-window.

## Resolved

### Entity pages took 20-60s: ArcadeDB's Cypher planner ignores unnamed anchors
**2026-08-12** | `search/{papers,pathways,traits,compounds,organisms,citations}.py`

**Symptom:** opening a paper on the dashboard took ~60s on the 298K-paper `human` graph,
~5s on `rice`. Every entity page was affected, not just Paper. The queries were already
anchored on an indexed unique key, so the shape looked correct.

**Root cause:** two separate planner rules in ArcadeDB's Cypher layer, both of which
silently degrade an indexed lookup to a full type scan.

1. **The anchor must be named.** `MATCH (:Paper {id: $id})-[:MENTIONS]->(g:Gene)` scans
   all 298K papers (20s); naming it — `(p:Paper {id: $id})` — resolves through
   `Paper[id]` (0.1s). Same query, same index, 200x. The name is otherwise unused, so
   it reads like a variable that could be tidied away. It cannot.
2. **The anchor must be written first when its peer is unbound.** With a labelled peer
   the planner picks the indexed side either way, but `MATCH (citing)-[:CITES]->(p:Paper
   {id: $id})` starts from `citing` and scans every vertex in the graph (29s). Written
   as `MATCH (p:Paper {id: $id})<-[:CITES]-(citing)` it is 0.09s.

A third, unrelated cause in the same page: `WHERE p.arxiv_id = $x OR p.pmid = $x` uses
neither index (each is indexed separately) and full-scanned Paper. Worse, the dashboard
passes the canonical `id` (`pmid:42508544`) while the query compared against the bare
`pmid`, so **References and Cited by were always empty** — the page reported "No citation
edges for this paper" for every paper, silently.

**Fix:** named every anchor and put it first; replaced the `OR` with
`citations.resolve_paper_id()`, which tries `id`, `pmid` and `arxiv_id` as separate
indexed lookups and then traverses from the resolved node. Paper page 60s → 1.7s
(`human`), 4.5s → 0.7s (`rice`).

**How to catch a recurrence:** grep for `(:Label {` — an unnamed anchor with an inline
predicate is always this bug. Compare any suspect Cypher against the same query in
ArcadeDB's native SQL (`SELECT ... FROM Paper WHERE id = ...`); a 100x+ gap means the
Cypher layer is scanning. See also the note in `stats.py::_rebuild_edge_counts`, which
is the same engine weakness in a different guise.

### ArcadeDB semantic search returned zero results on the large corpus
**2026-07-22, fixed 2026-08-06** | `search/semantic.py`, `LSM_VECTOR` index

On lg2 (291K vectors), `vector.neighbors` silently returned an empty set in 0.05s — no
error, no log line. Originally misdiagnosed as GC thrashing/latency; the multi-minute
stalls were the restart rebuild (open entry above), and the real defect was correctness:
tombstoned vectors scored `Infinity` similarity and crowded out real candidates.
**Fix:** upgraded to ArcadeDB 26.8.1. Verified 10/10 hits, self-match at distance 0.

### `enrich` crashed mid-run on a short Semantic Scholar batch response
**2026-07-27** | `ingest/semantic_scholar.py::_enrich_batch`

S2's batch endpoint can silently omit an id rather than return a `null` slot;
positional `zip(strict=True)` then raised and — under the cron script's `set -e` —
killed the whole daily pipeline. **Fix:** match responses back by `externalIds`; ids
with no item are treated as not-found.

### PubMed daily fetch silently stuck for a week on a future checkpoint date
**2026-07-27** | `ingest/pipeline.py::run_daily_fetch_pubmed`

A journal `PubDate` months in the future pushed the checkpoint forward, inverting the
query date range — zero results daily, reported as success. **Fix:** checkpoint only
advances on `published_date <= today`; checkpoint manually reset.

### arXiv `fetch-daily` 429s traced to cron timing, not code
**2026-07-22, root-caused 2026-07-27** | cron schedule

Six consecutive daily 429s on the first request. Cause: cron at 03:00 UTC = 23:00
Eastern, inside arXiv's nightly maintenance window; manual runs at other hours always
worked. **Fix:** rescheduled to 14:07 UTC. Lesson: check time-of-day correlation before
adding retry logic to a recurring failure.

### ArcadeDB 26.7.1: any write touching an embedded Paper failed at commit
**2026-07-16** | ArcadeDB 26.7.1, `LSM_VECTOR` index

Any transaction touching a Paper tracked by the vector index — even updating an
unrelated field — failed with `Timer already cancelled`, over both Bolt and SQL. Hit
enrich, fetch-daily, and backfill simultaneously. **Fix:** upgraded to 26.7.2. This bug
is why writes touching Paper vertices were moved to SQL/HTTP.

### RunPod embedding outage silently produced a mostly-unembedded corpus
**2026-07-15** | `ingest/pipeline.py`

No retry on the GPU embedding endpoint: one failure crashed a multi-hour backload; after
adding retries, a sustained outage left ~108K of ~110K papers unembedded with no flag.
**Fix:** tenacity retries + `backfill-embeddings` command. Operationally: compare
embedded count to paper count after any large run.

### `enrich` permanently stuck reprocessing the same papers
**2026-07-15** | `ingest/pipeline.py::run_enrichment`

Papers S2 reported not-found never got `enriched_at`, so every run re-selected the same
~25 papers and never advanced. **Fix:** not-found papers are stamped enriched too.

### `upsert_paper_stubs` silently dropped `pmid` from citation stubs
**2026-07-15** | `graph/upsert.py::upsert_paper_stubs`

A missing field in the stub-write parameters created PubMed stub nodes without their
`pmid`, breaking later lookups keyed on it. **Fix:** added the field.
