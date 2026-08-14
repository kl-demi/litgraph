"""Typer CLI for spokebio's biology layer: pathway/ontology reference data and
PubTator3 entity extraction. Mounted onto the main `litgraph` app as `litgraph bio`.
"""

import typer
from rich.console import Console

from spokebio.ingest.chebi_mesh_crosswalk import DEFAULT_MESH_YEAR
from spokebio.pipeline import (
    run_disease_ontology_ingest,
    run_go_ingest,
    run_pubtator_mentions,
    run_reactome_ingest,
    run_reactome_maps_to_ingest,
)
from spokebio.release_check import check_and_reingest
from spokebio.schema_ext import ensure_schema

app = typer.Typer(help="Biology-layer ingestion: pathways, ontologies, PubTator3 entity extraction.")
console = Console()


@app.command("go-pathways")
def go_pathways(
    obo_path: str = typer.Option(
        None, "--obo-path", help="Path to a local go-basic.obo (downloaded automatically if omitted/missing)"
    ),
    force_download: bool = typer.Option(False, "--force-download", help="Re-download even if already cached"),
    batch_size: int = typer.Option(500, "--batch-size"),
) -> None:
    """Ingest GO's biological_process branch as Pathway nodes."""
    ensure_schema()
    totals = run_go_ingest(obo_path=obo_path, batch_size=batch_size, force_download=force_download)
    console.print(
        f"[green]Processed {totals['pathways_processed']} GO terms, "
        f"+{totals['new_pathways']} new Pathway nodes.[/green]"
    )


@app.command("reactome-pathways")
def reactome_pathways(
    batch_size: int = typer.Option(500, "--batch-size"),
    force_download: bool = typer.Option(False, "--force-download", help="Re-download even if already cached"),
    mesh_year: int = typer.Option(
        DEFAULT_MESH_YEAR, "--mesh-year", help="MeSH publishes no stable 'current' URL -- bump if the default goes stale"
    ),
) -> None:
    """Ingest Reactome's human pathways, gene participation, compound production, and
    GO pathway correspondences. If pathways/PARTICIPATES_IN/PRODUCES are already loaded
    and you only need the MAPS_TO edges, use `reactome-maps-to` instead --
    PARTICIPATES_IN alone can take ~30 minutes to re-check on a full run."""
    ensure_schema()
    totals = run_reactome_ingest(batch_size=batch_size, force_download=force_download, mesh_year=mesh_year)
    console.print(
        f"[green]Processed {totals['pathways_processed']} pathways (+{totals['new_pathways']} new), "
        f"{totals['edges_processed']} gene-pathway pairs (+{totals['new_participates_in_edges']} new "
        f"PARTICIPATES_IN edges), {totals['produces_processed']} pathway-compound pairs "
        f"(+{totals['new_produces_edges']} new PRODUCES edges), {totals['go_mappings_processed']} "
        f"Reactome-GO pathway correspondences (+{totals['new_maps_to_edges']} new MAPS_TO edges).[/green]"
    )


@app.command("reactome-maps-to")
def reactome_maps_to(
    batch_size: int = typer.Option(500, "--batch-size"),
    force_download: bool = typer.Option(False, "--force-download", help="Re-download even if already cached"),
) -> None:
    """Ingest only Reactome<->GO pathway correspondences (MAPS_TO), skipping the slower
    pathways/PARTICIPATES_IN/PRODUCES passes `reactome-pathways` also does. Use this to
    backfill MAPS_TO onto a database that already has the rest of Reactome loaded."""
    ensure_schema()
    totals = run_reactome_maps_to_ingest(batch_size=batch_size, force_download=force_download)
    console.print(
        f"[green]Processed {totals['go_mappings_processed']} Reactome-GO pathway correspondences, "
        f"+{totals['new_maps_to_edges']} new MAPS_TO edges.[/green]"
    )


@app.command("disease-ontology")
def disease_ontology(
    obo_path: str = typer.Option(
        None, "--obo-path", help="Path to a local doid.obo (downloaded automatically if omitted/missing)"
    ),
    force_download: bool = typer.Option(False, "--force-download", help="Re-download even if already cached"),
    batch_size: int = typer.Option(500, "--batch-size"),
) -> None:
    """Ingest Disease Ontology as DOIDs and an is_a hierarchy over MeSH-keyed Disease nodes."""
    ensure_schema()
    totals = run_disease_ontology_ingest(obo_path=obo_path, batch_size=batch_size, force_download=force_download)
    console.print(
        f"[green]Processed {totals['xrefs_processed']} MeSH-mapped DO terms "
        f"(+{totals['new_diseases']} new Disease nodes), {totals['is_a_processed']} is_a claims "
        f"(+{totals['new_is_a_edges']} new IS_A edges).[/green]"
    )


@app.command("pubtator-mentions")
def pubtator_mentions(
    limit: int = typer.Option(500, "--limit", help="Max papers to process this run"),
    requests_per_second: float = typer.Option(
        3.0, "--requests-per-second", help="PubTator3 request rate ceiling -- conservative, no documented official limit"
    ),
) -> None:
    """Extract Gene/Compound/Organism/Disease mentions via PubTator3 for PubMed papers
    not yet checked by this extractor."""
    ensure_schema()
    totals = run_pubtator_mentions(limit=limit, requests_per_second=requests_per_second)
    console.print(
        f"[green]Processed {totals['papers_processed']} papers: +{totals['new_genes']} genes, "
        f"+{totals['new_compounds']} compounds, +{totals['new_organisms']} organisms, "
        f"+{totals['new_diseases']} diseases, +{totals['new_mention_edges']} MENTIONS edges, "
        f"named {totals['genes_named']} previously key-only genes.[/green]"
    )


_LABEL = {"go": "GO", "reactome": "Reactome", "disease_ontology": "Disease Ontology"}
_TOTALS_LINE = {
    "go": lambda t: f"Processed {t['pathways_processed']} GO terms, +{t['new_pathways']} new Pathway nodes.",
    "reactome": lambda t: (
        f"Processed {t['pathways_processed']} pathways (+{t['new_pathways']} new), "
        f"{t['edges_processed']} gene-pathway pairs (+{t['new_participates_in_edges']} new PARTICIPATES_IN "
        f"edges), {t['produces_processed']} pathway-compound pairs (+{t['new_produces_edges']} new "
        f"PRODUCES edges), {t['go_mappings_processed']} Reactome-GO pathway correspondences "
        f"(+{t['new_maps_to_edges']} new MAPS_TO edges)."
    ),
    "disease_ontology": lambda t: (
        f"Processed {t['xrefs_processed']} MeSH-mapped DO terms (+{t['new_diseases']} new Disease nodes), "
        f"{t['is_a_processed']} is_a claims (+{t['new_is_a_edges']} new IS_A edges)."
    ),
}


@app.command("check-releases")
def check_releases() -> None:
    """Check GO, Reactome, and Disease Ontology for a new release; re-ingest only
    the sources that changed. State is tracked per database -- see
    release_check.default_state_path."""
    ensure_schema()
    for source, result in check_and_reingest().items():
        label = _LABEL[source]
        if result["changed"]:
            console.print(
                f"[yellow]{label} release changed: {result['previous']!r} -> "
                f"{result['current']!r}; re-ingested[/yellow]"
            )
            console.print(_TOTALS_LINE[source](result["totals"]))
        else:
            console.print(f"{label} release unchanged ({result['current']}).")
