"""Bootstrap script: ingest Reactome's human pathways as Pathway nodes, and NCBI Gene ->
Pathway associations as PARTICIPATES_IN edges. See docs/spoke_schema.md for the full
design context (file structure, evidence-code handling, the base-vs-_All_Levels
tradeoff).

Downloads ReactomePathways.txt and NCBI2Reactome.txt (no license/API key needed) to
data/reactome/ on first run unless already cached there.

Creates Gene nodes on demand for any Reactome-referenced gene not already in the graph
(no `name` yet in that case -- MENTIONS backfills it later if the literature catches
up) -- most of Reactome's ~12K human genes won't already have a Gene node from
literature-derived MENTIONS alone.
"""

import argparse

from spokebio.pipeline import run_reactome_ingest
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download Reactome's files even if already cached locally"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    ensure_schema()
    totals = run_reactome_ingest(batch_size=args.batch_size, force_download=args.force_download)
    print(
        f"Processed {totals['pathways_processed']} human pathways (+{totals['new_pathways']} new) "
        f"and {totals['edges_processed']} gene-pathway pairs "
        f"(+{totals['new_participates_in_edges']} new PARTICIPATES_IN edges)."
    )


if __name__ == "__main__":
    main()
