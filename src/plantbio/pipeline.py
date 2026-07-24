from datetime import UTC, datetime

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from litgraph.db.neo4j_client import run_read
from plantbio.ingest.go import DEFAULT_OBO_PATH, ensure_obo_file, extract_pathways, iter_term_stanzas
from plantbio.ingest.pubtator import EXPORT_BATCH_SIZE, PubTatorClient
from plantbio.models import EntityMention, Pathway
from plantbio.upsert import mark_papers_checked, upsert_mentions, upsert_pathways

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
