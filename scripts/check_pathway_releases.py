"""Check whether GO or Reactome have published a new release since the last check, and
re-run their ingestion (with force_download=True) only if the release changed. Cheap to
run on a schedule -- a no-op when nothing has changed, no full re-download otherwise.

GO's release is read from the `data-version` line in go-basic.obo's own header, fetched
via a small byte-range GET (a few KB, not the full ~30MB file). Reactome's release is
read from its ContentService version endpoint. Both confirmed live 2026-07-27 by
inspection, not from published API docs for either source -- if either changes format,
this will raise rather than silently misreport the version.
"""

import argparse
import json
import re
from pathlib import Path

import httpx

from spokebio.pipeline import run_go_ingest, run_reactome_ingest
from spokebio.schema_ext import ensure_schema

STATE_PATH = Path("data/pathway_release_state.json")
GO_OBO_URL = "https://purl.obolibrary.org/obo/go/go-basic.obo"
REACTOME_VERSION_URL = "https://reactome.org/ContentService/data/database/version"


def get_go_release() -> str:
    resp = httpx.get(GO_OBO_URL, headers={"Range": "bytes=0-3000"}, follow_redirects=True, timeout=30.0)
    resp.raise_for_status()
    match = re.search(r"^data-version:\s*(\S+)", resp.text, re.MULTILINE)
    if not match:
        raise ValueError("data-version line not found in go-basic.obo header -- format may have changed")
    return match.group(1)


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
    current = {"go": get_go_release(), "reactome": get_reactome_release()}

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

    save_state(current)


if __name__ == "__main__":
    main()
