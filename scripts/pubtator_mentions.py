"""Bootstrap script: for each ingested PubMed paper PubTator3 hasn't been queried for
yet, fetch its Gene/Chemical/Species entity mentions and write them as MENTIONS edges.
See docs/plant_schema.md for the full design context, filter-policy rationale (why
Disease is dropped, why unnormalized entities are skipped), and the live API test this
was built from.

Safe to run alongside another ingestion job (e.g. `litgraph enrich`) against the same
ArcadeDB instance -- see plantbio/upsert.py's docstring for why. Start with a small
--limit on a first run against a live/shared box before scaling up.
"""

import argparse

from plantbio.pipeline import run_pubtator_mentions
from plantbio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=500, help="Max papers to process this run (default: 500)")
    parser.add_argument(
        "--requests-per-second",
        type=float,
        default=3.0,
        help="PubTator3 request rate ceiling (default: 3.0 -- conservative, no documented official limit)",
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_pubtator_mentions(limit=args.limit, requests_per_second=args.requests_per_second)
    print(
        f"Processed {totals['papers_processed']} papers: "
        f"+{totals['new_genes']} genes, +{totals['new_compounds']} compounds, "
        f"+{totals['new_organisms']} organisms, +{totals['new_mention_edges']} MENTIONS edges."
    )


if __name__ == "__main__":
    main()
