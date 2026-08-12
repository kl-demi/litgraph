"""Check whether GO, Reactome, or Disease Ontology have published a new release since the
last check, and re-run their ingestion (with force_download=True) only if the release
changed. Cheap to run on a schedule -- a no-op when nothing has changed, no full
re-download otherwise.

GO's and DO's releases are read from the `data-version` line in their own OBO headers,
fetched via a small byte-range GET (3KB, not the full ~30MB/~7MB file; both servers were
confirmed to honour Range). Reactome's release is read from its ContentService version
endpoint. All confirmed live by inspection -- GO/Reactome 2026-07-27, DO 2026-08-12 --
not from published API docs, so if any changes format this raises rather than silently
misreporting the version.
"""

import argparse
import json
import re
from pathlib import Path

import httpx

from spokebio.pipeline import run_disease_ontology_ingest, run_go_ingest, run_reactome_ingest
from spokebio.schema_ext import ensure_schema

STATE_PATH = Path("data/pathway_release_state.json")
GO_OBO_URL = "https://purl.obolibrary.org/obo/go/go-basic.obo"
DOID_OBO_URL = "https://purl.obolibrary.org/obo/doid.obo"
REACTOME_VERSION_URL = "https://reactome.org/ContentService/data/database/version"


def _obo_data_version(url: str, filename: str) -> str:
    resp = httpx.get(url, headers={"Range": "bytes=0-3000"}, follow_redirects=True, timeout=30.0)
    resp.raise_for_status()
    match = re.search(r"^data-version:\s*(\S+)", resp.text, re.MULTILINE)
    if not match:
        raise ValueError(f"data-version line not found in {filename} header -- format may have changed")
    return match.group(1)


def get_go_release() -> str:
    return _obo_data_version(GO_OBO_URL, "go-basic.obo")


def get_disease_ontology_release() -> str:
    return _obo_data_version(DOID_OBO_URL, "doid.obo")


def get_reactome_release() -> str:
    resp = httpx.get(REACTOME_VERSION_URL, timeout=30.0)
    resp.raise_for_status()
    return resp.text.strip()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def main() -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()

    state = load_state()
    current = {
        "go": get_go_release(),
        "reactome": get_reactome_release(),
        "disease_ontology": get_disease_ontology_release(),
    }

    if current["go"] != state.get("go"):
        print(f"GO release changed: {state.get('go')!r} -> {current['go']!r}; re-ingesting")
        ensure_schema()
        totals = run_go_ingest(force_download=True)
        print(f"Processed {totals['pathways_processed']} GO terms, +{totals['new_pathways']} new Pathway nodes.")
    else:
        print(f"GO release unchanged ({current['go']}).")

    if current["reactome"] != state.get("reactome"):
        print(f"Reactome release changed: {state.get('reactome')!r} -> {current['reactome']!r}; re-ingesting")
        ensure_schema()
        totals = run_reactome_ingest(force_download=True)
        print(
            f"Processed {totals['pathways_processed']} pathways (+{totals['new_pathways']} new), "
            f"{totals['edges_processed']} gene-pathway pairs (+{totals['new_participates_in_edges']} new)."
        )
    else:
        print(f"Reactome release unchanged ({current['reactome']}).")

    if current["disease_ontology"] != state.get("disease_ontology"):
        print(
            f"Disease Ontology release changed: {state.get('disease_ontology')!r} -> "
            f"{current['disease_ontology']!r}; re-ingesting"
        )
        ensure_schema()
        totals = run_disease_ontology_ingest(force_download=True)
        print(
            f"Processed {totals['xrefs_processed']} MeSH-mapped DO terms "
            f"(+{totals['new_diseases']} new Disease nodes), {totals['is_a_processed']} is_a "
            f"claims (+{totals['new_is_a_edges']} new IS_A edges)."
        )
    else:
        print(f"Disease Ontology release unchanged ({current['disease_ontology']}).")

    save_state(current)


if __name__ == "__main__":
    main()
