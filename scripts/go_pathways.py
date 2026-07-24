"""Bootstrap script: ingest GO's biological_process branch as Pathway nodes -- the
species-agnostic half of pathway ingestion (see docs/plant_schema.md's Pathway scope
note). PlantCyc/MetaCyc's species-specific pathways are a separate pass, deferred until
its license agreement is submitted and PGDB files are downloaded (plantcyc.org's
license form is free but requires your own name/institution -- not something this
script can do for you).

Downloads go-basic.obo (~30MB, no license/API key needed) to data/go-basic.obo on first
run unless --obo-path points at an already-downloaded copy.
"""

import argparse

from plantbio.pipeline import run_go_ingest
from plantbio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--obo-path", default=None, help="Path to a local go-basic.obo (downloaded automatically if omitted/missing)")
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download go-basic.obo even if already cached locally"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    ensure_schema()
    totals = run_go_ingest(obo_path=args.obo_path, batch_size=args.batch_size, force_download=args.force_download)
    print(f"Processed {totals['pathways_processed']} GO biological_process terms, {totals['new_pathways']} new Pathway nodes.")


if __name__ == "__main__":
    main()
