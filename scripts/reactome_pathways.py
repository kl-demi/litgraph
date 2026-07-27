"""Bootstrap script: ingest Reactome's human pathways as Pathway nodes; NCBI Gene ->
Pathway associations as PARTICIPATES_IN edges; and Pathway -> Compound associations as
PRODUCES edges (resolved through the ChEBI<->MeSH crosswalk -- only ~33.7% of
referenced ChEBI ids resolve, the rest are silently dropped, not a bug). See
docs/spoke_schema.md for the full design context.

Downloads Reactome's flat files, ChEBI's compounds/database_accession files, MeSH's
descriptor/supplementary-concept files, and Biomappings' curated mappings (no
license/API key needed for any of them) on first run unless already cached locally.

Creates Gene/Compound nodes on demand for any Reactome-referenced entity not already in
the graph (no `name` yet in that case -- MENTIONS backfills it later if the literature
catches up) -- most of Reactome's referenced genes/compounds won't already have a node
from literature-derived MENTIONS alone.
"""

import argparse

from spokebio.ingest.chebi_mesh_crosswalk import DEFAULT_MESH_YEAR
from spokebio.pipeline import run_reactome_ingest
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download all source files even if already cached locally"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--mesh-year",
        type=int,
        default=DEFAULT_MESH_YEAR,
        help="MeSH publishes no stable 'current' URL -- bump this if the default year's files go stale",
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_reactome_ingest(batch_size=args.batch_size, force_download=args.force_download, mesh_year=args.mesh_year)
    print(
        f"Processed {totals['pathways_processed']} human pathways (+{totals['new_pathways']} new), "
        f"{totals['edges_processed']} gene-pathway pairs (+{totals['new_participates_in_edges']} new PARTICIPATES_IN edges), "
        f"and {totals['produces_processed']} pathway-compound pairs (+{totals['new_produces_edges']} new PRODUCES edges)."
    )


if __name__ == "__main__":
    main()
