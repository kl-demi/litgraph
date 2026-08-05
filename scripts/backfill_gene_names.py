"""Backfill: give Gene nodes a readable display name, from reference files directly.

Genes bootstrapped by the GAF and Oryzabase loaders are created key-only, because those
sources are keyed on locus ids and carry no symbol -- so queries return `gene: null` even
though the graph knows the gene. No extraction involved here; this resolves each
`ncbigene:<id>` against the reference files.

Sources in priority order:
  1. Oryzabase CGSNL symbol   -- rice's nomenclature authority (SD1, GHD7, XA21, SUB1A).
                                 Does not feed NCBI, hence absent from gene_info.
  2. gene_info real Symbol    -- for rice, mostly organellar genes.
  3. gene_info locus id       -- RAP-DB preferred, else MSU/TIGR.

NCBI's `LOC<GeneID>` placeholder is never written: it restates the key and would hide which
genes genuinely lack a symbol.

Also upgrades names that are still just a locus id when a curated symbol is now available,
verified against what is actually stored -- filling nulls alone would leave genes displaying
a bare Os08g0238500 forever, since the fill is null-only.

Safe to re-run: idempotent, and it never overwrites a curator- or extractor-assigned name.
"""

import argparse

from spokebio.ingest.oryzabase import DEFAULT_ORYZABASE_PATH
from spokebio.pipeline import run_gene_name_backfill
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--organism", default="Oryza_sativa", help="NCBI gene_info filename stem (default: Oryza_sativa)"
    )
    parser.add_argument(
        "--oryzabase-path",
        default=DEFAULT_ORYZABASE_PATH,
        help=f"Path to the Oryzabase gene list (default: {DEFAULT_ORYZABASE_PATH})",
    )
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download both inputs even if already cached locally"
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_gene_name_backfill(
        organism=args.organism,
        oryzabase_path=args.oryzabase_path,
        batch_size=args.batch_size,
        force_download=args.force_download,
    )
    print(
        f"Named {totals['genes_named']} key-only Gene nodes and upgraded "
        f"{totals['genes_upgraded']} locus-id names to curated symbols, "
        f"from {totals['candidates']} genes with a usable name."
    )


if __name__ == "__main__":
    main()
