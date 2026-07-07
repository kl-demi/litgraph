from datetime import date, datetime

import typer
from rich.console import Console
from rich.table import Table

from arxiv_graphdb.config import get_settings
from arxiv_graphdb.db.schema import ensure_schema
from arxiv_graphdb.ingest.pipeline import run_backload, run_daily_fetch, run_enrichment

app = typer.Typer(help="arXiv paper ingestion & search backed by Neo4j.")
search_app = typer.Typer(help="Query the graph.")
app.add_typer(search_app, name="search")

console = Console()


def _parse_categories(categories: str | None) -> list[str] | None:
    if not categories:
        return None
    return [c.strip() for c in categories.split(",") if c.strip()]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


@app.command("init-db")
def init_db() -> None:
    """Create Neo4j constraints and indexes (idempotent)."""
    ensure_schema()
    console.print("[green]Schema ensured (constraints + full-text + vector indexes).[/green]")


@app.command()
def backload(
    file: str = typer.Option(..., "--file", help="Path to the Kaggle arxiv-metadata-oai-snapshot.json[.gz]"),
    categories: str = typer.Option(None, "--categories", help="Comma-separated category prefixes, e.g. cs.CL,cs.LG"),
    start_date: str = typer.Option(None, "--start-date", help="YYYY-MM-DD"),
    end_date: str = typer.Option(None, "--end-date", help="YYYY-MM-DD"),
    limit: int = typer.Option(None, "--limit", help="Max papers to ingest"),
    batch_size: int = typer.Option(200, "--batch-size"),
) -> None:
    """Backload historical papers from the Kaggle arXiv metadata snapshot."""
    settings = get_settings()
    cats = _parse_categories(categories) or settings.default_categories_list
    total = run_backload(
        file,
        categories=cats,
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
        limit=limit,
        batch_size=batch_size,
    )
    console.print(f"[green]Backloaded {total} papers.[/green]")


@app.command("fetch-daily")
def fetch_daily(
    categories: str = typer.Option(None, "--categories", help="Comma-separated arXiv categories, e.g. cs.CL,cs.LG"),
    batch_size: int = typer.Option(200, "--batch-size"),
) -> None:
    """Fetch new papers submitted since the last run, via the arXiv API."""
    settings = get_settings()
    cats = _parse_categories(categories) or settings.default_categories_list
    total = run_daily_fetch(cats, batch_size=batch_size)
    console.print(f"[green]Fetched {total} new papers.[/green]")


@app.command()
def enrich(
    limit: int = typer.Option(500, "--limit", help="Max not-yet-enriched papers to process"),
) -> None:
    """Enrich papers with Semantic Scholar citation data."""
    total = run_enrichment(limit=limit)
    console.print(f"[green]Enriched {total} papers.[/green]")


@search_app.command("keyword")
def search_keyword(query: str, top_k: int = typer.Option(10, "--top-k")) -> None:
    from arxiv_graphdb.search.keyword import keyword_search

    _print_results(keyword_search(query, top_k=top_k))


@search_app.command("semantic")
def search_semantic(query: str, top_k: int = typer.Option(10, "--top-k")) -> None:
    from arxiv_graphdb.search.semantic import semantic_search

    _print_results(semantic_search(query, top_k=top_k))


@app.command()
def citations(
    arxiv_id: str,
    direction: str = typer.Option("both", "--direction", help="cites | cited-by | both"),
    depth: int = typer.Option(1, "--depth", help="Hops for neighborhood traversal (1-3)"),
) -> None:
    """Show citation graph around a paper."""
    from arxiv_graphdb.search.citations import citation_neighborhood, get_citing_papers, get_references

    if direction in ("cites", "both"):
        console.print(f"[bold]Papers {arxiv_id} cites:[/bold]")
        _print_results(get_references(arxiv_id))
    if direction in ("cited-by", "both"):
        console.print(f"[bold]Papers citing {arxiv_id}:[/bold]")
        _print_results(get_citing_papers(arxiv_id))
    if depth > 1:
        console.print(f"[bold]{depth}-hop neighborhood:[/bold]")
        _print_results(citation_neighborhood(arxiv_id, depth=depth))


def _print_results(rows: list[dict]) -> None:
    if not rows:
        console.print("[yellow]No results.[/yellow]")
        return
    table = Table()
    for key in rows[0]:
        table.add_column(key)
    for row in rows:
        table.add_row(*[str(row.get(key, "")) for key in rows[0]])
    console.print(table)


if __name__ == "__main__":
    app()
