"""Bootstrap script: ingest Disease Ontology as DOIDs and an is_a hierarchy over the
MeSH-keyed Disease nodes PubTator3 produces.

DO maps only ~30% of its terms to MeSH, covering 62% of real disease mentions, so this
enriches Disease rather than re-keying it: DOID lands as a property, and the hierarchy is
projected into MeSH space by walking through unmapped intermediate terms. Diseases DO
does not map keep their MeSH id and stay in the graph unlinked.

MERGE-based and safe to re-run. Downloads doid.obo (~7MB, no license/API key needed) to
data/doid.obo on first run unless --obo-path points at an already-downloaded copy.
"""

import argparse

from spokebio.pipeline import run_disease_ontology_ingest
from spokebio.schema_ext import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--obo-path", default=None, help="Path to a local doid.obo (downloaded automatically if omitted/missing)")
    parser.add_argument(
        "--force-download", action="store_true", help="Re-download doid.obo even if already cached locally"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    ensure_schema()
    totals = run_disease_ontology_ingest(
        obo_path=args.obo_path, batch_size=args.batch_size, force_download=args.force_download
    )
    print(
        f"Processed {totals['xrefs_processed']} MeSH-mapped DO terms "
        f"({totals['new_diseases']} new Disease nodes), "
        f"{totals['is_a_processed']} is_a claims ({totals['new_is_a_edges']} new IS_A edges)."
    )


if __name__ == "__main__":
    main()
