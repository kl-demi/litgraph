"""Bootstrap script: ingest one species' GO annotations (GAF) as Gene -> Pathway
PARTICIPATES_IN edges.

Use this instead of scripts/reactome_pathways.py for any non-human corpus. Reactome's
current release covers 16 species and no plants at all, so running the Reactome script
against e.g. a rice graph writes human pathways and edges into it.

Run scripts/go_pathways.py first -- the edge upsert MATCHes Pathway nodes rather than
creating them, so without the GO terms loaded this writes nothing.

Downloads both inputs on first run (no license/API key needed):
  - GO:   <species_code>-uniprot.gaf.gz     -> data/gaf/
  - NCBI: <organism>.gene_info.gz           -> data/gene_info/

Defaults are Oryza sativa Japonica Group (ORYSJ / taxon 39947).
"""

import argparse

from spokebio.ingest.gaf import DEFAULT_SPECIES_CODE
from spokebio.pipeline import run_gaf_ingest
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--species-code",
        default=DEFAULT_SPECIES_CODE,
        help=f"UniProt mnemonic naming the GAF, e.g. ORYSJ, ARATH, ZEAMA (default: {DEFAULT_SPECIES_CODE})",
    )
    parser.add_argument(
        "--organism",
        default="Oryza_sativa",
        help="NCBI gene_info filename stem for the same species (default: Oryza_sativa)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download both inputs even if already cached locally"
    )
    args = parser.parse_args()

    ensure_schema()
    totals = run_gaf_ingest(
        species_code=args.species_code,
        organism=args.organism,
        batch_size=args.batch_size,
        force_download=args.force_download,
    )
    print(
        f"Processed {totals['edges_processed']} gene-pathway pairs from "
        f"{totals['rows_considered']} biological_process annotations "
        f"({totals['dropped_unresolved']} unresolvable, {totals['dropped_negated']} NOT-qualified), "
        f"{totals['new_participates_in_edges']} new PARTICIPATES_IN edges."
    )


if __name__ == "__main__":
    main()
