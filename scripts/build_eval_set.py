"""Build a hand-labelling worksheet for measuring rice gene extraction accuracy.

Produces two CSVs under --output-dir (default data/eval/):

  precision.csv -- one row per candidate gene mention, with the sentence that justifies it.
                   Fill in the `correct` column with y or n. Rows are drawn from two tiers:
                     conservative     = the policy live in the graph today
                     permissive_only  = the extra matches --include-unaudited would add,
                                        i.e. the ones held back as unverified
                   Judging a row takes seconds, so this gets the large sample.

  recall.csv    -- one row per sampled paper with its full abstract, and the genes the
                   gazetteer found already pre-filled. List any it MISSED in
                   `missed_genes` (comma-separated); leave blank if it missed none, but
                   put something in `notes` so a reviewed-and-clean row is distinguishable
                   from an unreviewed one. Slower per row, so this gets the small sample.

Then score with: uv run python scripts/score_eval_set.py

Sampling is deterministic for a given --seed, so rebuilding won't reshuffle work you've
already done. Existing files are never overwritten unless --force is passed.
"""

import argparse
from pathlib import Path

from litgraph.db import arcadedb_http
from spokebio.eval import (
    STRATUM_CONSERVATIVE,
    STRATUM_NO_MATCH,
    STRATUM_PERMISSIVE_ONLY,
    build_candidates,
    display_symbols,
    has_usable_abstract,
    sample,
    stratify,
    write_precision_worksheet,
    write_recall_worksheet,
)
from spokebio.ingest.gene_crosswalk import build_locus_identifier_crosswalk, ensure_gene_info_file
from spokebio.ingest.gene_gazetteer import build_gazetteer
from spokebio.ingest.oryzabase import DEFAULT_ORYZABASE_PATH, ensure_oryzabase_file

_PAGE = """
SELECT id, pmid, title, abstract FROM Paper
WHERE is_stub = false AND abstract IS NOT NULL
ORDER BY id SKIP :skip LIMIT :limit
"""


def _all_papers(page_size: int) -> list[dict]:
    papers: list[dict] = []
    skip = 0
    while True:
        rows = arcadedb_http.run_query(_PAGE, skip=skip, limit=page_size)
        if not rows:
            return papers
        papers.extend(rows)
        skip += page_size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", default="data/eval")
    parser.add_argument(
        "--per-tier", type=int, default=80, help="Precision rows (judgements) per tier (default: 80)"
    )
    parser.add_argument("--recall-papers", type=int, default=30, help="Papers on the recall sheet (default: 30)")
    parser.add_argument("--seed", type=int, default=20260803, help="Sampling seed; keep it stable across rebuilds")
    parser.add_argument("--page-size", type=int, default=4000)
    parser.add_argument("--force", action="store_true", help="Overwrite existing worksheets")
    args = parser.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    precision_path, recall_path = out / "precision.csv", out / "recall.csv"
    existing = [p for p in (precision_path, recall_path) if p.exists()]
    if existing and not args.force:
        raise SystemExit(f"refusing to overwrite {', '.join(str(p) for p in existing)} -- pass --force if intended")

    crosswalk = build_locus_identifier_crosswalk(ensure_gene_info_file("Oryza_sativa"))
    oryzabase = ensure_oryzabase_file(DEFAULT_ORYZABASE_PATH)
    conservative = build_gazetteer(oryzabase, crosswalk)
    permissive = build_gazetteer(oryzabase, crosswalk, include_unaudited=True)
    print(f"conservative: {len(conservative)} forms | permissive: {len(permissive)} forms")

    papers = _all_papers(args.page_size)
    print(f"loaded {len(papers)} papers with abstracts")
    buckets = stratify(papers, conservative, permissive)
    for name, rows in buckets.items():
        print(f"  {name:<16} {len(rows)} papers")

    # Sample papers generously, build their candidates, then sample the *candidates* down to
    # the target. Two reasons: a paper mentioning 12 genes must not dominate the worksheet,
    # and --per-tier should bound the number of judgements asked of a human, not the number of
    # papers behind them (75 papers per tier produced 267 rows, which is a different job).
    cons_papers = sample(buckets[STRATUM_CONSERVATIVE], args.per_tier * 2, args.seed)
    perm_papers = sample(buckets[STRATUM_PERMISSIVE_ONLY], args.per_tier * 2, args.seed + 1)
    candidates = sample(build_candidates(cons_papers, conservative, STRATUM_CONSERVATIVE), args.per_tier, args.seed + 4)
    candidates += sample(
        build_candidates(perm_papers, permissive, STRATUM_PERMISSIVE_ONLY, exclude=conservative),
        args.per_tier,
        args.seed + 5,
    )

    symbols = display_symbols(conservative)
    n_precision = write_precision_worksheet(precision_path, candidates, symbols)

    # Weighted toward no-match papers: those are where misses actually live. Filtered to
    # papers with a real abstract -- some records carry a non-null but blank one.
    no_match = [p for p in buckets[STRATUM_NO_MATCH] if has_usable_abstract(p)]
    matched = [p for p in buckets[STRATUM_CONSERVATIVE] if has_usable_abstract(p)]
    recall_papers = sample(no_match, args.recall_papers * 2 // 3, args.seed + 2)
    recall_papers += sample(matched, args.recall_papers - len(recall_papers), args.seed + 3)
    n_recall = write_recall_worksheet(recall_path, recall_papers, conservative)

    print(f"\nwrote {n_precision} precision rows -> {precision_path}")
    print(f"wrote {n_recall} recall rows      -> {recall_path}")
    print("\nFill in `correct` (y/n) in precision.csv, and `missed_genes` in recall.csv.")
    print("Then: uv run python scripts/score_eval_set.py")


if __name__ == "__main__":
    main()
