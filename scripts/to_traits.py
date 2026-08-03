"""Bootstrap script: ingest the Trait Ontology (TO) as Trait nodes.

The trait vocabulary a trait-centric query resolves against. Run this before
scripts/oryzabase_traits.py -- the ASSOCIATED_WITH edge upsert MATCHes Trait nodes
rather than creating them, so without the terms loaded it writes nothing.

Downloads to.obo on first run (~1MB, no license/API key needed) to data/to.obo.
Only TO's own terms become Trait nodes: to.obo also carries imported PO/CHEBI/GO/PATO
terms, which are filtered out -- see ingest/trait_ontology.py.
"""

import argparse

from spokebio.pipeline import run_to_ingest
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--obo-path", default=None, help="Override the local to.obo path (default: data/to.obo)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download to.obo even if already cached locally"
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_to_ingest(
        obo_path=args.obo_path, batch_size=args.batch_size, force_download=args.force_download
    )
    print(
        f"Processed {totals['traits_processed']} TO terms, "
        f"{totals['new_traits']} new Trait nodes."
    )


if __name__ == "__main__":
    main()
