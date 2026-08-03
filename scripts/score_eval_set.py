"""Score the hand-labelled worksheets from scripts/build_eval_set.py.

Reports precision per tier and an estimate of recall. The decision this exists to inform:
whether the `permissive_only` tier is precise enough to load with --include-unaudited, or
whether it should stay held back for LLM disambiguation.

Safe to run on a partially-filled worksheet -- unlabelled rows are counted as skipped, never
guessed at.
"""

import argparse
import csv
from pathlib import Path

from spokebio.eval import score_precision, score_recall


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", default="data/eval")
    args = parser.parse_args()
    d = Path(args.input_dir)

    precision_rows = _read(d / "precision.csv")
    if precision_rows:
        print("PRECISION")
        scored = score_precision(precision_rows)
        for tier in sorted(scored):
            s = scored[tier]
            note = "" if s["labelled"] else "   <- nothing labelled yet"
            print(
                f"  {tier:<16} precision={s['precision']:.1%}  "
                f"({s['correct']}/{s['labelled']} correct, {s['skipped']} unlabelled){note}"
            )
        cons, perm = scored.get("conservative"), scored.get("permissive_only")
        if cons and perm and cons["labelled"] and perm["labelled"]:
            print(
                f"\n  The decision: permissive_only is {perm['precision']:.1%} precise vs "
                f"{cons['precision']:.1%} for what's live.\n"
                f"  Widening adds ~8,600 mentions across ~3,600 more papers at that precision."
            )
    else:
        print(f"PRECISION: no rows found at {d / 'precision.csv'} -- run build_eval_set.py first")

    recall_rows = _read(d / "recall.csv")
    if recall_rows:
        r = score_recall(recall_rows)
        print("\nRECALL")
        print(f"  papers reviewed  {r['papers_reviewed']} ({r['papers_skipped']} not yet reviewed)")
        print(f"  mentions found   {r['mentions_found']}")
        print(f"  mentions missed  {r['mentions_missed']}")
        if r["papers_reviewed"]:
            print(f"  recall           {r['recall']:.1%}")
    else:
        print(f"\nRECALL: no rows found at {d / 'recall.csv'}")


if __name__ == "__main__":
    main()
