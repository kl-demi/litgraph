"""Backfill: attach the community locus id to Gene nodes as a secondary lookup key.

`gene_id` (`ncbigene:`) stays the one canonical unique key, so this never creates or re-keys
a node -- it adds `locus_id` alongside, indexed NOTUNIQUE. See docs/plant_schema.md's Gene ID
crosswalk note, option 2.

NOTUNIQUE is required rather than cautious: 103 of rice's 26,977 locus ids map to more than
one NCBI gene (e.g. Os03g0120900 -> ncbigene:4324719 and ncbigene:4331436), usually the same
locus entered twice across assembly revisions. `upsert.find_genes_by_locus_id` therefore
returns a list per locus id rather than collapsing to an arbitrary one.

RAP-DB is preferred over MSU/TIGR, matching the display-name tiers -- RAP-DB annotates the
current IRGSP-1.0 reference, and the two systems are largely disjoint (22,459 rice gene_info
rows carry a RAP id, 3,464 an MSU id, only 419 both).

Idempotent and null-only: a locus id is a stable fact, so re-running never thrashes one that
is already set.
"""

import argparse

from spokebio.pipeline import run_gene_locus_backfill
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--organism", default="Oryza_sativa", help="NCBI gene_info filename stem (default: Oryza_sativa)"
    )
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download gene_info even if already cached locally"
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_gene_locus_backfill(
        organism=args.organism, batch_size=args.batch_size, force_download=args.force_download
    )
    print(
        f"Assigned locus_id to {totals['genes_assigned']} Gene nodes, "
        f"from {totals['candidates']} genes with a locus id in gene_info."
    )


if __name__ == "__main__":
    main()
