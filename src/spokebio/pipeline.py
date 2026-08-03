from datetime import UTC, datetime

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from litgraph.db.neo4j_client import run_read
from spokebio.ingest.chebi_mesh_crosswalk import (
    DEFAULT_MESH_YEAR,
    build_crosswalk,
    ensure_biomappings_file,
    ensure_chebi_file,
    ensure_mesh_file,
)
from spokebio.ingest.gaf import DEFAULT_SPECIES_CODE, ensure_gaf_file
from spokebio.ingest.gaf import extract_participates_in as extract_gaf_participates_in
from spokebio.ingest.gene_crosswalk import (
    build_gene_identifier_crosswalk,
    build_locus_identifier_crosswalk,
    ensure_gene_info_file,
)
from spokebio.ingest.go import DEFAULT_OBO_PATH, ensure_obo_file, extract_pathways, iter_term_stanzas
from spokebio.ingest.oryzabase import (
    DEFAULT_ORYZABASE_PATH,
    ensure_oryzabase_file,
    extract_associated_with,
)
from spokebio.ingest.pubtator import EXPORT_BATCH_SIZE, PubTatorClient
from spokebio.ingest.reactome import (
    ensure_reactome_file,
    extract_human_pathways,
    extract_participates_in,
    extract_produces,
)
from spokebio.ingest.trait_ontology import DEFAULT_TO_OBO_PATH, ensure_to_obo_file, extract_traits
from spokebio.models import AssociatedWith, EntityMention, ParticipatesIn, Pathway, Produces, Trait
from spokebio.upsert import (
    mark_papers_checked,
    upsert_associated_with,
    upsert_mentions,
    upsert_participates_in,
    upsert_pathways,
    upsert_produces,
    upsert_traits,
)

console = Console()

# OPTIONAL MATCH + WHERE IS NULL rather than a NOT EXISTS{} subquery or NOT (p)-[]->() --
# ArcadeDB's Cypher layer has documented quirks with pattern-matching inside other
# constructs (see graph/upsert.py's _UPSERT_CATEGORIES comment), so this sticks to the
# plainest Cypher shape already proven to work elsewhere in the codebase.
_FIND_UNCHECKED = """
MATCH (p:Paper)
WHERE p.is_stub = false AND p.pmid IS NOT NULL
OPTIONAL MATCH (checked:PubtatorChecked {paper_id: p.id})
WITH p, checked
WHERE checked IS NULL
RETURN p.id AS id, p.pmid AS pmid
LIMIT $limit
"""


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def _flush(batch: dict[str, list[EntityMention]], totals: dict[str, int]) -> None:
    stats = upsert_mentions(batch)
    mark_papers_checked(list(batch), datetime.now(UTC))
    totals["papers_processed"] += len(batch)
    for key in ("new_organisms", "new_genes", "new_compounds", "new_mention_edges"):
        totals[key] += stats[key]


def run_pubtator_mentions(limit: int = 500, requests_per_second: float = 3.0) -> dict[str, int]:
    """For up to ``limit`` ingested PubMed papers PubTator3 hasn't been queried for yet,
    fetch its Gene/Chemical/Species annotations and upsert surviving ones as MENTIONS
    edges.

    Deliberately conservative so this can run alongside another ingestion job (e.g.
    `litgraph enrich`) against the same ArcadeDB instance: never SETs a property on a
    Paper vertex (see upsert.py), and paces PubTator3 requests at
    ``requests_per_second`` rather than firing batches back-to-back.
    """
    rows = run_read(_FIND_UNCHECKED, limit=limit)
    totals = {"papers_processed": 0, "new_organisms": 0, "new_genes": 0, "new_compounds": 0, "new_mention_edges": 0}
    if not rows:
        console.log("pubtator-mentions: nothing to do")
        return totals

    pmid_to_paper_id = {r["pmid"]: r["id"] for r in rows}

    with PubTatorClient(requests_per_second=requests_per_second) as client, _progress() as progress:
        task = progress.add_task("Fetching PubTator3 mentions", total=len(pmid_to_paper_id))
        batch: dict[str, list[EntityMention]] = {}
        for pmid, mentions in client.fetch_mentions(list(pmid_to_paper_id)):
            paper_id = pmid_to_paper_id.pop(pmid, None)
            if paper_id is None:
                continue
            batch[paper_id] = mentions
            if len(batch) >= EXPORT_BATCH_SIZE:
                _flush(batch, totals)
                progress.update(task, advance=len(batch))
                batch = {}
        if batch:
            _flush(batch, totals)
            progress.update(task, advance=len(batch))

        # Any pmid PubTator3 never returned a document for (dropped silently by its
        # API) still needs marking checked, or it reappears at the front of
        # _FIND_UNCHECKED's LIMIT window on every future run.
        never_returned = list(pmid_to_paper_id.values())
        if never_returned:
            mark_papers_checked(never_returned, datetime.now(UTC))
            totals["papers_processed"] += len(never_returned)

    console.log(
        f"pubtator-mentions: processed {totals['papers_processed']} papers -- "
        f"+{totals['new_genes']} genes, +{totals['new_compounds']} compounds, "
        f"+{totals['new_organisms']} organisms, +{totals['new_mention_edges']} MENTIONS edges"
    )
    return totals


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


def run_reactome_ingest(
    batch_size: int = 500, force_download: bool = False, mesh_year: int = DEFAULT_MESH_YEAR
) -> dict[str, int]:
    """Ingest Reactome's human pathways (ReactomePathways.txt) as Pathway nodes;
    NCBI Gene -> Pathway associations (NCBI2Reactome.txt, the base file -- not
    _All_Levels, see docs/spoke_schema.md's open-decision note on that tradeoff) as
    PARTICIPATES_IN edges; and ChEBI2Reactome.txt's Pathway -> Compound associations as
    PRODUCES edges, resolved through the ChEBI<->MeSH crosswalk (see
    ingest/chebi_mesh_crosswalk.py -- only ~33.7% of referenced ChEBI ids resolve;
    the rest are silently dropped, not a bug).

    Unlike the GO/PubTator pieces, this creates Gene/Compound nodes on demand (see
    upsert.py's docstrings) -- most of Reactome's human genes/compounds won't already
    have a node from literature-derived MENTIONS alone.
    """
    pathways_path = ensure_reactome_file("ReactomePathways.txt", force=force_download)
    edges_path = ensure_reactome_file("NCBI2Reactome.txt", force=force_download)
    chebi_edges_path = ensure_reactome_file("ChEBI2Reactome.txt", force=force_download)

    totals = {
        "pathways_processed": 0,
        "new_pathways": 0,
        "edges_processed": 0,
        "new_participates_in_edges": 0,
        "produces_processed": 0,
        "new_produces_edges": 0,
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

    edges = extract_participates_in(edges_path)
    with _progress() as progress:
        task = progress.add_task("Writing PARTICIPATES_IN edges", total=len(edges))
        edge_batch: list[ParticipatesIn] = []
        for edge in edges:
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
        f"reactome-participates-in: processed {totals['edges_processed']} gene-pathway pairs, "
        f"+{totals['new_participates_in_edges']} new PARTICIPATES_IN edges"
    )

    compounds_path = ensure_chebi_file("compounds.tsv.gz", force=force_download)
    database_accession_path = ensure_chebi_file("database_accession.tsv.gz", force=force_download)
    mesh_d_path = ensure_mesh_file(f"d{mesh_year}.bin", year=mesh_year, force=force_download)
    mesh_c_path = ensure_mesh_file(f"c{mesh_year}.bin", year=mesh_year, force=force_download)
    biomappings_path = ensure_biomappings_file(force=force_download)
    crosswalk = build_crosswalk(compounds_path, database_accession_path, [mesh_d_path, mesh_c_path], biomappings_path)

    produces_edges = extract_produces(chebi_edges_path, crosswalk)
    with _progress() as progress:
        task = progress.add_task("Writing PRODUCES edges", total=len(produces_edges))
        produces_batch: list[Produces] = []
        for edge in produces_edges:
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
        f"reactome-produces: processed {totals['produces_processed']} pathway-compound pairs, "
        f"+{totals['new_produces_edges']} new PRODUCES edges"
    )
    return totals


def run_gaf_ingest(
    species_code: str = DEFAULT_SPECIES_CODE,
    organism: str = "Oryza_sativa",
    batch_size: int = 500,
    force_download: bool = False,
) -> dict[str, int]:
    """Ingest one species' GO annotations as Gene -> Pathway PARTICIPATES_IN edges.

    The non-human path to pathway edges: Reactome doesn't cover plant species, so
    ``run_reactome_ingest`` can't serve a plant corpus. Pairs GO's per-species GAF 
    with NCBI's gene_info file for the same species to
    resolve gene references onto the existing ``ncbigene:`` Gene keys.

    Requires ``run_go_ingest`` to have run first -- the PARTICIPATES_IN upsert MATCHes
    Pathway nodes rather than creating them, so edges to absent terms are silently
    dropped.
    """
    gaf_path = ensure_gaf_file(species_code, force=force_download)
    gene_info_path = ensure_gene_info_file(organism, force=force_download)

    crosswalk = build_gene_identifier_crosswalk(gene_info_path)
    console.log(f"gaf-participates-in: {len(crosswalk)} gene identifiers in the {organism} crosswalk")

    extraction = extract_gaf_participates_in(gaf_path, crosswalk)
    resolved = extraction.rows_considered - extraction.dropped_negated - extraction.dropped_unresolved
    console.log(
        f"gaf-participates-in: {extraction.rows_considered} biological_process rows -- "
        f"dropped {extraction.dropped_negated} NOT-qualified, "
        f"{extraction.dropped_unresolved} unresolvable to a gene "
        f"({extraction.dropped_unresolved / extraction.rows_considered:.1%}), "
        f"{extraction.dropped_duplicate} duplicate pairs; {resolved} annotations "
        f"-> {len(extraction.edges)} distinct edges"
    )

    totals = {
        "rows_considered": extraction.rows_considered,
        "dropped_negated": extraction.dropped_negated,
        "dropped_unresolved": extraction.dropped_unresolved,
        "edges_processed": 0,
        "new_participates_in_edges": 0,
    }

    with _progress() as progress:
        task = progress.add_task("Writing PARTICIPATES_IN edges (GAF)", total=len(extraction.edges))
        batch: list[ParticipatesIn] = []
        for edge in extraction.edges:
            batch.append(edge)
            if len(batch) >= batch_size:
                totals["new_participates_in_edges"] += upsert_participates_in(batch)
                totals["edges_processed"] += len(batch)
                progress.update(task, advance=len(batch))
                batch = []
        if batch:
            totals["new_participates_in_edges"] += upsert_participates_in(batch)
            totals["edges_processed"] += len(batch)
            progress.update(task, advance=len(batch))

    console.log(
        f"gaf-participates-in: processed {totals['edges_processed']} gene-pathway pairs, "
        f"+{totals['new_participates_in_edges']} new PARTICIPATES_IN edges"
    )
    return totals


def run_to_ingest(
    obo_path: str | None = None, batch_size: int = 500, force_download: bool = False
) -> dict[str, int]:
    """Ingest the Trait Ontology as Trait nodes -- the trait vocabulary a trait-centric
    query resolves against.

    Must run before ``run_oryzabase_ingest``: the ASSOCIATED_WITH upsert MATCHes Trait
    nodes rather than creating them, so edges to absent terms are silently dropped (same
    ordering constraint as run_go_ingest -> run_gaf_ingest).

    No Paper interaction at all -- pure Trait-node upserts -- so this is safe to run
    alongside another ingestion job against the same ArcadeDB instance.
    """
    path = ensure_to_obo_file(obo_path or DEFAULT_TO_OBO_PATH, force=force_download)
    totals = {"traits_processed": 0, "new_traits": 0}

    with _progress() as progress:
        task = progress.add_task("Ingesting Trait Ontology terms", total=None)
        batch: list[Trait] = []
        for trait in extract_traits(iter_term_stanzas(path)):
            batch.append(trait)
            if len(batch) >= batch_size:
                totals["new_traits"] += upsert_traits(batch)
                totals["traits_processed"] += len(batch)
                progress.update(task, advance=len(batch))
                batch = []
        if batch:
            totals["new_traits"] += upsert_traits(batch)
            totals["traits_processed"] += len(batch)
            progress.update(task, advance=len(batch))

    console.log(
        f"to-traits: processed {totals['traits_processed']} TO terms, "
        f"+{totals['new_traits']} new Trait nodes"
    )
    return totals


def run_oryzabase_ingest(
    oryzabase_path: str | None = None,
    organism: str = "Oryza_sativa",
    to_obo_path: str | None = None,
    batch_size: int = 500,
    force_download: bool = False,
) -> dict[str, int]:
    """Ingest Oryzabase's curated rice gene-trait annotations as Gene -> Trait
    ASSOCIATED_WITH edges.

    Pairs Oryzabase's gene list with NCBI's gene_info for the same species to resolve
    each gene reference onto the existing ``ncbigene:`` Gene keys, so trait edges land on
    the same nodes GAF's PARTICIPATES_IN edges and PubTator's MENTIONS edges use -- that
    shared key is what makes Trait <- Gene -> Pathway traversable.

    Uses ``build_locus_identifier_crosswalk`` rather than the GAF path's
    ``build_gene_identifier_crosswalk``: rice RAP-DB ids live in gene_info's
    Other_designations column, which the latter doesn't index (20.2% vs 81.5%
    resolution).

    Requires ``run_to_ingest`` to have run first.
    """
    path = ensure_oryzabase_file(oryzabase_path or DEFAULT_ORYZABASE_PATH, force=force_download)
    gene_info_path = ensure_gene_info_file(organism, force=force_download)

    crosswalk = build_locus_identifier_crosswalk(gene_info_path)
    console.log(f"oryzabase-traits: {len(crosswalk)} gene identifiers in the {organism} crosswalk")

    # The Trait ids that actually exist as nodes, so annotations against TO terms
    # obsoleted since Oryzabase wrote them get counted rather than silently vanishing in
    # the upsert's MATCH. Reads the same cached to.obo run_to_ingest used.
    known_trait_ids = {
        t.trait_id for t in extract_traits(iter_term_stanzas(ensure_to_obo_file(to_obo_path or DEFAULT_TO_OBO_PATH)))
    }

    extraction = extract_associated_with(path, crosswalk, known_trait_ids=known_trait_ids)
    console.log(
        f"oryzabase-traits: {extraction.rows_with_traits} rows with a TO annotation -- "
        f"dropped {extraction.dropped_unresolved} unresolvable to a gene "
        f"({extraction.dropped_unresolved / max(extraction.rows_with_traits, 1):.1%}), "
        f"{extraction.dropped_unknown_trait} annotations to obsolete/unknown TO terms, "
        f"{extraction.dropped_duplicate} duplicate pairs "
        f"-> {len(extraction.edges)} distinct edges"
    )

    totals = {
        "rows_with_traits": extraction.rows_with_traits,
        "dropped_unresolved": extraction.dropped_unresolved,
        "dropped_unknown_trait": extraction.dropped_unknown_trait,
        "edges_processed": 0,
        "new_associated_with_edges": 0,
    }

    with _progress() as progress:
        task = progress.add_task("Writing ASSOCIATED_WITH edges", total=len(extraction.edges))
        batch: list[AssociatedWith] = []
        for edge in extraction.edges:
            batch.append(edge)
            if len(batch) >= batch_size:
                totals["new_associated_with_edges"] += upsert_associated_with(batch)
                totals["edges_processed"] += len(batch)
                progress.update(task, advance=len(batch))
                batch = []
        if batch:
            totals["new_associated_with_edges"] += upsert_associated_with(batch)
            totals["edges_processed"] += len(batch)
            progress.update(task, advance=len(batch))

    console.log(
        f"oryzabase-traits: processed {totals['edges_processed']} gene-trait pairs, "
        f"+{totals['new_associated_with_edges']} new ASSOCIATED_WITH edges"
    )
    return totals
