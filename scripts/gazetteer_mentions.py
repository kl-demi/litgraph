"""Bootstrap script: extract rice gene mentions from paper text with the Oryzabase
gazetteer, writing Paper -> Gene MENTIONS edges stamped source="oryzabase-gazetteer".

Exists because PubTator3's gene NER barely fires on rice literature (7.4% of papers, and
only 4.9% of the genes it names are actually rice genes). Complements PubTator rather than
replacing it -- only adds edges that don't already exist.

**Run with --dry-run first.** A gazetteer's precision depends entirely on how much its
source vocabulary overlaps ordinary English, and the report's most-matched-forms list is
where that failure shows up first.

Requires scripts/oryzabase_traits.py's inputs (the Oryzabase export and NCBI gene_info),
which it downloads if absent. Safe to re-run: MENTIONS upserts are create-if-missing.
"""

import argparse

from spokebio.pipeline import run_gazetteer_mentions
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--oryzabase-path", default=None, help="Override the local TSV path (default: data/oryzabase/gene_list.tsv)"
    )
    parser.add_argument(
        "--organism", default="Oryza_sativa", help="NCBI gene_info filename stem (default: Oryza_sativa)"
    )
    parser.add_argument("--page-size", type=int, default=2000, help="Papers fetched per query page")
    parser.add_argument("--batch-size", type=int, default=500, help="Papers per upsert batch")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be written without writing anything"
    )
    parser.add_argument(
        "--include-unaudited",
        action="store_true",
        help="Add the permissive tier: any 4+ char letters+digits symbol, minus units and audited "
        "rejects. Reaches ~22%% of papers instead of ~15%%, but ~36%% of its matches are unverified. "
        "For generating LLM-disambiguation candidates, not routine loading.",
    )
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download both inputs even if already cached locally"
    )
    args = parser.parse_args()

    if not args.dry_run:
        ensure_schema()
    totals = run_gazetteer_mentions(
        oryzabase_path=args.oryzabase_path,
        organism=args.organism,
        page_size=args.page_size,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        include_unaudited=args.include_unaudited,
        force_download=args.force_download,
    )
    scanned = max(totals["papers_scanned"], 1)
    print(
        f"Scanned {totals['papers_scanned']} papers; {totals['papers_with_mentions']} "
        f"({totals['papers_with_mentions'] / scanned:.1%}) had >=1 rice gene mention, "
        f"{totals['mentions_found']} mentions found, "
        f"{totals['new_mention_edges']} new MENTIONS edges, {totals['new_genes']} new Gene nodes."
    )


if __name__ == "__main__":
    main()
