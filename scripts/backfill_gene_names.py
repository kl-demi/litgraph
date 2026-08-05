"""Backfill: give a readable display name to Gene nodes that have none, from NCBI's
gene_info file for the species.

Genes bootstrapped by the GAF and Oryzabase loaders are created key-only, because those
sources are keyed on locus ids and carry no symbol -- so queries return `gene: null` for
most rows even though the graph knows the gene. This resolves each `ncbigene:<id>` against
gene_info directly, no extraction involved.

Two tiers, and deliberately no third (see ingest/gene_crosswalk.build_gene_name_map):
  1. the real Symbol, when NCBI has one
  2. otherwise the RAP-DB locus id (e.g. Os01g0970700) from Other_designations

Genes whose only offer is NCBI's `LOC<id>` placeholder are skipped, so they stay null and
remain fillable later. Writing `LOC4338919` would restate the key, hide which genes truly
lack a symbol, and permanently block a real symbol (the fill is null-only).

**Run this last**, after PubTator and the Oryzabase gazetteer. Because the fill only
touches nulls, a locus id written here would pre-empt a real symbol from an extraction
pass that runs afterwards.
"""

import argparse

from spokebio.ingest.gene_crosswalk import DEFAULT_ORGANISM, build_gene_name_map, ensure_gene_info_file
from spokebio.schema_ext import ensure_schema
from spokebio.upsert import backfill_gene_names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--organism",
        default="Oryza_sativa",
        help=f"NCBI gene_info filename stem, e.g. Oryza_sativa, {DEFAULT_ORGANISM} (default: Oryza_sativa)",
    )
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download gene_info even if already cached locally"
    )
    args = parser.parse_args()

    ensure_schema()
    path = ensure_gene_info_file(args.organism, force=args.force_download)
    names = build_gene_name_map(path)
    print(f"{len(names)} of {args.organism}'s genes have a usable name in gene_info.")

    named = 0
    items = list(names.items())
    for start in range(0, len(items), args.batch_size):
        named += backfill_gene_names(dict(items[start : start + args.batch_size]))

    print(f"Named {named} previously key-only Gene nodes (nodes with a name already set were left alone).")


if __name__ == "__main__":
    main()
