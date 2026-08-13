from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from spokebio.extract import run_extraction
from spokebio.ingest.chebi_mesh_crosswalk import (
    DEFAULT_MESH_YEAR,
    build_crosswalk,
    ensure_biomappings_file,
    ensure_chebi_file,
    ensure_mesh_file,
)
from spokebio.ingest.disease_ontology import (
    DEFAULT_DOID_PATH,
    ensure_doid_file,
    extract_disease_xrefs,
    extract_is_a_edges,
)
from spokebio.ingest.disease_ontology import iter_term_stanzas as iter_doid_stanzas
from spokebio.ingest.go import DEFAULT_OBO_PATH, ensure_obo_file, extract_pathways, iter_term_stanzas
from spokebio.ingest.pubtator import PubTatorExtractor
from spokebio.ingest.reactome import (
    ensure_reactome_file,
    extract_human_pathways,
    extract_participates_in,
    extract_pathway_go_mappings,
    extract_produces,
)
from spokebio.models import DiseaseIsA, DiseaseXref, ParticipatesIn, Pathway, PathwayGoMapping, Produces
from spokebio.upsert import (
    upsert_disease_is_a,
    upsert_disease_xrefs,
    upsert_participates_in,
    upsert_pathway_go_mappings,
    upsert_pathways,
    upsert_produces,
)

console = Console()

def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def run_pubtator_mentions(limit: int = 500, requests_per_second: float = 3.0) -> dict[str, int]:
    """Run the PubTator3 extractor over up to ``limit`` unchecked PubMed papers.

    Deliberately conservative so this can run alongside another ingestion job (e.g.
    `litgraph enrich`) against the same ArcadeDB instance: never SETs a property on a
    Paper vertex (see upsert.py), and paces PubTator3 requests at
    ``requests_per_second`` rather than firing batches back-to-back.
    """
    return run_extraction(PubTatorExtractor(requests_per_second=requests_per_second), limit=limit)


def run_go_ingest(
    obo_path: str | None = None, batch_size: int = 500, force_download: bool = False
) -> dict[str, int]:
    """Ingest GO's biological_process branch as Pathway nodes -- the species-agnostic
    half of pathway ingestion (docs/plant_schema.md; PlantCyc/MetaCyc's species-specific
    pathways are a separate, not-yet-built pass pending its license/PGDB files).

    Downloads go-basic.obo to ``obo_path`` (default: data/go-basic.obo) if not already
    cached there. No Paper interaction at all -- pure Pathway-node upserts -- so this
    carries no risk to any other job running against the same ArcadeDB instance.
    """
    path = ensure_obo_file(obo_path or DEFAULT_OBO_PATH, force=force_download)
    totals = {"pathways_processed": 0, "new_pathways": 0}

    with _progress() as progress:
        task = progress.add_task("Ingesting GO biological_process terms", total=None)
        batch: list[Pathway] = []
        for pathway in extract_pathways(iter_term_stanzas(path)):
            batch.append(pathway)
            if len(batch) >= batch_size:
                totals["new_pathways"] += upsert_pathways(batch)
                totals["pathways_processed"] += len(batch)
                progress.update(task, advance=len(batch))
                batch = []
        if batch:
            totals["new_pathways"] += upsert_pathways(batch)
            totals["pathways_processed"] += len(batch)
            progress.update(task, advance=len(batch))

    console.log(
        f"go-pathways: processed {totals['pathways_processed']} biological_process terms, "
        f"+{totals['new_pathways']} new Pathway nodes"
    )
    return totals


def run_disease_ontology_ingest(
    obo_path: str | None = None, batch_size: int = 500, force_download: bool = False
) -> dict[str, int]:
    """Ingest Disease Ontology as DOIDs and an is_a hierarchy over MeSH-keyed Disease
    nodes (see ingest/disease_ontology.py for why Disease stays MeSH-keyed).

    Two passes, xrefs before edges, since the edge pass bootstraps neither endpoint.
    Downloads doid.obo to ``obo_path`` (default: data/doid.obo) if not already cached
    there. No Paper interaction, so this is safe alongside another ingestion job.
    """
    path = ensure_doid_file(obo_path or DEFAULT_DOID_PATH, force=force_download)
    totals = {"xrefs_processed": 0, "new_diseases": 0, "is_a_processed": 0, "new_is_a_edges": 0}

    with _progress() as progress:
        task = progress.add_task("Ingesting Disease Ontology xrefs", total=None)
        batch: list[DiseaseXref] = []
        for xref in extract_disease_xrefs(iter_doid_stanzas(path)):
            batch.append(xref)
            if len(batch) >= batch_size:
                totals["new_diseases"] += upsert_disease_xrefs(batch)
                totals["xrefs_processed"] += len(batch)
                progress.update(task, advance=len(batch))
                batch = []
        if batch:
            totals["new_diseases"] += upsert_disease_xrefs(batch)
            totals["xrefs_processed"] += len(batch)
            progress.update(task, advance=len(batch))

        task = progress.add_task("Ingesting Disease Ontology hierarchy", total=None)
        edges: list[DiseaseIsA] = []
        for edge in extract_is_a_edges(iter_doid_stanzas(path)):
            edges.append(edge)
            if len(edges) >= batch_size:
                totals["new_is_a_edges"] += upsert_disease_is_a(edges)
                totals["is_a_processed"] += len(edges)
                progress.update(task, advance=len(edges))
                edges = []
        if edges:
            totals["new_is_a_edges"] += upsert_disease_is_a(edges)
            totals["is_a_processed"] += len(edges)
            progress.update(task, advance=len(edges))

    console.log(
        f"disease-ontology: processed {totals['xrefs_processed']} MeSH-mapped terms "
        f"(+{totals['new_diseases']} new Disease nodes), {totals['is_a_processed']} is_a "
        f"claims (+{totals['new_is_a_edges']} new IS_A edges)"
    )
    return totals


def run_reactome_ingest(
    batch_size: int = 500, force_download: bool = False, mesh_year: int = DEFAULT_MESH_YEAR
) -> dict[str, int]:
    """Ingest Reactome's human pathways (ReactomePathways.txt) as Pathway nodes;
    NCBI Gene -> Pathway associations (NCBI2Reactome.txt, the base file -- not
    _All_Levels, see docs/spoke_schema.md's open-decision note on that tradeoff) as
    PARTICIPATES_IN edges; ChEBI2Reactome.txt's Pathway -> Compound associations as
    PRODUCES edges, resolved through the ChEBI<->MeSH crosswalk (see
    ingest/chebi_mesh_crosswalk.py -- only ~33.7% of referenced ChEBI ids resolve;
    unresolved ones are dropped and counted, see ``dropped_unresolved`` below); and
    Pathways2GoTerms_human.txt's Reactome Pathway -> GO Pathway correspondences as
    MAPS_TO edges (silently dropped where `run_go_ingest` hasn't created the GO side --
    order-independent since a later re-run of either picks up the rest).

    Unlike the GO/PubTator pieces, this creates Gene/Compound nodes on demand (see
    upsert.py's docstrings) -- most of Reactome's human genes/compounds won't already
    have a node from literature-derived MENTIONS alone.
    """
    pathways_path = ensure_reactome_file("ReactomePathways.txt", force=force_download)
    edges_path = ensure_reactome_file("NCBI2Reactome.txt", force=force_download)
    chebi_edges_path = ensure_reactome_file("ChEBI2Reactome.txt", force=force_download)
    go_mapping_path = ensure_reactome_file("Pathways2GoTerms_human.txt", force=force_download)

    totals = {
        "pathways_processed": 0,
        "new_pathways": 0,
        "edges_processed": 0,
        "new_participates_in_edges": 0,
        "dropped_duplicate_participates_in": 0,
        "produces_processed": 0,
        "new_produces_edges": 0,
        "dropped_unresolved_produces": 0,
        "dropped_duplicate_produces": 0,
        "go_mappings_processed": 0,
        "new_maps_to_edges": 0,
    }

    with _progress() as progress:
        task = progress.add_task("Ingesting Reactome human pathways", total=None)
        batch: list[Pathway] = []
        for pathway in extract_human_pathways(pathways_path):
            batch.append(pathway)
            if len(batch) >= batch_size:
                totals["new_pathways"] += upsert_pathways(batch)
                totals["pathways_processed"] += len(batch)
                progress.update(task, advance=len(batch))
                batch = []
        if batch:
            totals["new_pathways"] += upsert_pathways(batch)
            totals["pathways_processed"] += len(batch)
            progress.update(task, advance=len(batch))

    console.log(
        f"reactome-pathways: processed {totals['pathways_processed']} human pathways, "
        f"+{totals['new_pathways']} new Pathway nodes"
    )

    extraction = extract_participates_in(edges_path)
    totals["dropped_duplicate_participates_in"] = extraction.dropped_duplicate
    with _progress() as progress:
        task = progress.add_task("Writing PARTICIPATES_IN edges", total=len(extraction.edges))
        edge_batch: list[ParticipatesIn] = []
        for edge in extraction.edges:
            edge_batch.append(edge)
            if len(edge_batch) >= batch_size:
                totals["new_participates_in_edges"] += upsert_participates_in(edge_batch)
                totals["edges_processed"] += len(edge_batch)
                progress.update(task, advance=len(edge_batch))
                edge_batch = []
        if edge_batch:
            totals["new_participates_in_edges"] += upsert_participates_in(edge_batch)
            totals["edges_processed"] += len(edge_batch)
            progress.update(task, advance=len(edge_batch))

    console.log(
        f"reactome-participates-in: {extraction.rows_considered} human gene-pathway rows -- "
        f"{extraction.dropped_duplicate} duplicate pairs collapsed by evidence rank; "
        f"processed {totals['edges_processed']} distinct pairs, "
        f"+{totals['new_participates_in_edges']} new PARTICIPATES_IN edges"
    )

    compounds_path = ensure_chebi_file("compounds.tsv.gz", force=force_download)
    database_accession_path = ensure_chebi_file("database_accession.tsv.gz", force=force_download)
    mesh_d_path = ensure_mesh_file(f"d{mesh_year}.bin", year=mesh_year, force=force_download)
    mesh_c_path = ensure_mesh_file(f"c{mesh_year}.bin", year=mesh_year, force=force_download)
    biomappings_path = ensure_biomappings_file(force=force_download)
    crosswalk = build_crosswalk(compounds_path, database_accession_path, [mesh_d_path, mesh_c_path], biomappings_path)

    produces_extraction = extract_produces(chebi_edges_path, crosswalk)
    totals["dropped_unresolved_produces"] = produces_extraction.dropped_unresolved
    totals["dropped_duplicate_produces"] = produces_extraction.dropped_duplicate
    with _progress() as progress:
        task = progress.add_task("Writing PRODUCES edges", total=len(produces_extraction.edges))
        produces_batch: list[Produces] = []
        for edge in produces_extraction.edges:
            produces_batch.append(edge)
            if len(produces_batch) >= batch_size:
                totals["new_produces_edges"] += upsert_produces(produces_batch)
                totals["produces_processed"] += len(produces_batch)
                progress.update(task, advance=len(produces_batch))
                produces_batch = []
        if produces_batch:
            totals["new_produces_edges"] += upsert_produces(produces_batch)
            totals["produces_processed"] += len(produces_batch)
            progress.update(task, advance=len(produces_batch))

    console.log(
        f"reactome-produces: {produces_extraction.rows_considered} human pathway-compound rows -- "
        f"dropped {produces_extraction.dropped_unresolved} unresolvable to a Compound "
        f"({produces_extraction.dropped_unresolved / max(produces_extraction.rows_considered, 1):.1%}), "
        f"{produces_extraction.dropped_duplicate} duplicate pairs collapsed by evidence rank; "
        f"processed {totals['produces_processed']} distinct pairs, "
        f"+{totals['new_produces_edges']} new PRODUCES edges"
    )

    go_mappings = extract_pathway_go_mappings(go_mapping_path)
    with _progress() as progress:
        task = progress.add_task("Writing MAPS_TO edges", total=len(go_mappings))
        mapping_batch: list[PathwayGoMapping] = []
        for mapping in go_mappings:
            mapping_batch.append(mapping)
            if len(mapping_batch) >= batch_size:
                totals["new_maps_to_edges"] += upsert_pathway_go_mappings(mapping_batch)
                totals["go_mappings_processed"] += len(mapping_batch)
                progress.update(task, advance=len(mapping_batch))
                mapping_batch = []
        if mapping_batch:
            totals["new_maps_to_edges"] += upsert_pathway_go_mappings(mapping_batch)
            totals["go_mappings_processed"] += len(mapping_batch)
            progress.update(task, advance=len(mapping_batch))

    console.log(
        f"reactome-maps-to: processed {totals['go_mappings_processed']} Reactome-GO pathway "
        f"correspondences, +{totals['new_maps_to_edges']} new MAPS_TO edges (rows whose GO side "
        f"isn't a biological_process Pathway node yet are silently skipped)"
    )
    return totals
