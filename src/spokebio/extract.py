"""Extractor interface and the generic fetch-extract-upsert loop over unchecked papers.

Exports:
    Extractor: protocol an entity extractor implements (name, requires, extract).
    run_extraction(extractor, limit): find papers the extractor hasn't checked, extract
        mentions, upsert them, and mark the papers checked.

Usage: implement Extractor (e.g. pubtator.PubTatorExtractor), then call
`run_extraction(MyExtractor(), limit=500)` from a pipeline job or script.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Protocol

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from litgraph.db.neo4j_client import run_read
from spokebio.models import EntityMention
from spokebio.upsert import mark_papers_checked, upsert_mentions

console = Console()

_FLUSH_SIZE = 100

# OPTIONAL MATCH + WHERE IS NULL rather than a NOT EXISTS{} subquery -- ArcadeDB's Cypher
# layer has documented quirks with pattern-matching inside other constructs (see
# graph/upsert.py's _UPSERT_CATEGORIES comment).
_FIND_UNCHECKED_TEMPLATE = """
MATCH (p:Paper)
WHERE p.is_stub = false{requires}
OPTIONAL MATCH (checked:ExtractionChecked {{extractor: $extractor, paper_id: p.id}})
WITH p, checked
WHERE checked IS NULL
RETURN p.id AS id{fields}
LIMIT $limit
"""


class Extractor(Protocol):
    """An entity extractor that turns papers into normalized EntityMentions.

    name: stable identifier, e.g. "pubtator3". Keys the ExtractionChecked bookkeeping and
        is stamped as MENTIONS.source -- when two extractors produce the same
        (paper, entity) edge, the first writer keeps the attribution (see upsert_mentions).
    requires: Paper properties that must be non-null for a paper to be a candidate,
        e.g. ("pmid",) for an API keyed on PMIDs, ("abstract",) for a text-based one.
        The candidate rows passed to `extract` carry `id` plus these properties.
    """

    name: str
    requires: tuple[str, ...]

    def extract(self, papers: list[dict]) -> Iterator[tuple[str, list[EntityMention]]]:
        """Yield (Paper.id, mentions) per paper. An empty list means "checked, nothing
        found"; a paper never yielded at all is still marked checked by the loop."""
        ...


def run_extraction(extractor: Extractor, limit: int = 500) -> dict[str, int]:
    """Run `extractor` over up to `limit` papers it hasn't checked yet.

    Mentions are upserted in batches with `source=extractor.name`; every candidate paper
    is marked checked afterwards, whether or not the extractor yielded anything for it,
    so it never reappears at the front of the next run's candidate window.
    """
    rows = run_read(
        _find_unchecked_query(extractor.requires), extractor=extractor.name, limit=limit
    )
    totals = {
        "papers_processed": 0, "new_organisms": 0, "new_genes": 0, "new_compounds": 0,
        "new_mention_edges": 0, "genes_named": 0,
    }
    if not rows:
        console.log(f"extract[{extractor.name}]: nothing to do")
        return totals

    pending = {row["id"] for row in rows}

    def flush(batch: dict[str, list[EntityMention]]) -> None:
        stats = upsert_mentions(batch, source=extractor.name)
        mark_papers_checked(extractor.name, list(batch), datetime.now(UTC))
        totals["papers_processed"] += len(batch)
        for key in ("new_organisms", "new_genes", "new_compounds", "new_mention_edges", "genes_named"):
            totals[key] += stats[key]

    with _progress() as progress:
        task = progress.add_task(f"Extracting mentions ({extractor.name})", total=len(rows))
        batch: dict[str, list[EntityMention]] = {}
        for paper_id, mentions in extractor.extract(rows):
            if paper_id not in pending:
                continue
            pending.discard(paper_id)
            batch[paper_id] = mentions
            if len(batch) >= _FLUSH_SIZE:
                flush(batch)
                progress.update(task, advance=len(batch))
                batch = {}
        if batch:
            flush(batch)
            progress.update(task, advance=len(batch))

        # Papers the extractor never yielded (e.g. PMIDs PubTator3 silently omits) still
        # need marking checked, or they reappear at the front of every future run.
        if pending:
            mark_papers_checked(extractor.name, list(pending), datetime.now(UTC))
            totals["papers_processed"] += len(pending)

    console.log(
        f"extract[{extractor.name}]: processed {totals['papers_processed']} papers -- "
        f"+{totals['new_genes']} genes, +{totals['new_compounds']} compounds, "
        f"+{totals['new_organisms']} organisms, +{totals['new_mention_edges']} MENTIONS edges, "
        f"named {totals['genes_named']} previously key-only genes"
    )
    return totals


def _find_unchecked_query(requires: tuple[str, ...]) -> str:
    conditions = "".join(f" AND p.{prop} IS NOT NULL" for prop in requires)
    fields = "".join(f", p.{prop} AS {prop}" for prop in requires)
    return _FIND_UNCHECKED_TEMPLATE.format(requires=conditions, fields=fields)


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
