"""Bootstrap script: ingest Oryzabase's curated rice gene-trait annotations as
Gene -> Trait ASSOCIATED_WITH edges.

Run scripts/to_traits.py first -- the edge upsert MATCHes Trait nodes rather than
creating them, so without the TO terms loaded this writes nothing.

Rice-specific by construction: Oryzabase is a rice database, and the gene resolution
depends on NCBI's Oryza_sativa gene_info file. There is no generic species switch here
the way scripts/gaf_participates_in.py has one.

Downloads both inputs on first run (no license/API key needed for the download, but
check Oryzabase's citation terms before republishing anything derived from it):
  - Oryzabase: gene list TSV (~11MB) -> data/oryzabase/
  - NCBI:      Oryza_sativa.gene_info.gz -> data/gene_info/
"""

import argparse

from spokebio.pipeline import run_oryzabase_ingest
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--oryzabase-path", default=None, help="Override the local TSV path (default: data/oryzabase/gene_list.tsv)"
    )
    parser.add_argument(
        "--organism", default="Oryza_sativa", help="NCBI gene_info filename stem (default: Oryza_sativa)"
    )
    parser.add_argument("--to-obo-path", default=None, help="Override the local to.obo path (default: data/to.obo)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download both inputs even if already cached locally"
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_oryzabase_ingest(
        oryzabase_path=args.oryzabase_path,
        organism=args.organism,
        to_obo_path=args.to_obo_path,
        batch_size=args.batch_size,
        force_download=args.force_download,
    )
    print(
        f"Processed {totals['edges_processed']} gene-trait pairs from "
        f"{totals['rows_with_traits']} rows with a TO annotation "
        f"({totals['dropped_unresolved']} unresolvable, "
        f"{totals['dropped_unknown_trait']} to obsolete TO terms), "
        f"{totals['new_associated_with_edges']} new ASSOCIATED_WITH edges."
    )


if __name__ == "__main__":
    main()
