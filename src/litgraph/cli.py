from datetime import date, datetime

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from litgraph.config import get_settings
from litgraph.db.schema import ensure_schema
from litgraph.ingest.pipeline import (
    run_backload,
    run_backload_pubmed,
    run_daily_fetch,
    run_daily_fetch_pubmed,
    run_enrichment,
)


app = typer.Typer(help="Academic paper ingestion & search backed by ArcadeDB (or Neo4j, see README).")
search_app = typer.Typer(help="Query the graph.")
app.add_typer(search_app, name="search")
stats_app = typer.Typer(help="Inspect what's in the graph.")
app.add_typer(stats_app, name="stats")

console = Console()

# -------------------------------- HELPERS -------------------------------------
def _parse_categories(categories: str | None) -> list[str] | None:
    if not categories:
        return None
    return [c.strip() for c in categories.split(",") if c.strip()]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()

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

# -------------------------------- MAIN APP ------------------------------------
@app.command("init-db")
def init_db() -> None:
    """Create the graph database's constraints and indexes (idempotent)."""
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


@app.command("backload-pubmed")
def backload_pubmed(
    dir: str = typer.Option(..., "--dir", help="Directory (or glob) of NCBI pubmed*.xml[.gz] baseline/update files"),
    mesh_terms: str = typer.Option(
        None, "--mesh-terms", help="Comma-separated MeSH headings to match, e.g. Anatomy,Phenomena and Processes"
    ),
    start_date: str = typer.Option(None, "--start-date", help="YYYY-MM-DD"),
    end_date: str = typer.Option(None, "--end-date", help="YYYY-MM-DD"),
    limit: int = typer.Option(None, "--limit", help="Max papers to ingest"),
    batch_size: int = typer.Option(200, "--batch-size"),
) -> None:
    """Backload historical papers from NCBI's PubMed baseline/update XML files."""
    terms = _parse_categories(mesh_terms)
    total = run_backload_pubmed(
        dir,
        mesh_terms=terms,
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
        limit=limit,
        batch_size=batch_size,
    )
    console.print(f"[green]Backloaded {total} PubMed papers.[/green]")


@app.command("fetch-daily-pubmed")
def fetch_daily_pubmed(
    mesh_terms: str = typer.Option(None, "--mesh-terms", help="PubMed query string, e.g. a MeSH term expression"),
    batch_size: int = typer.Option(200, "--batch-size"),
) -> None:
    """Fetch new papers published since the last run, via NCBI E-utilities."""
    settings = get_settings()
    query = mesh_terms or settings.default_pubmed_mesh_terms
    total = run_daily_fetch_pubmed(query, batch_size=batch_size)
    console.print(f"[green]Fetched {total} new PubMed papers.[/green]")


@app.command()
def enrich(
    limit: int = typer.Option(500, "--limit", help="Max not-yet-enriched papers to process"),
) -> None:
    """Enrich papers with Semantic Scholar citation data."""
    total = run_enrichment(limit=limit)
    console.print(f"[green]Enriched {total} papers.[/green]")


@app.command()
def citations(
    paper_id: str = typer.Argument(..., help="An arXiv id (e.g. 2101.00001) or a PubMed PMID (e.g. 12345678)"),
    direction: str = typer.Option("both", "--direction", help="cites | cited-by | both"),
    depth: int = typer.Option(1, "--depth", help="Hops for neighborhood traversal (1-3)"),
) -> None:
    """Show citation graph around a paper, looked up by arXiv id or PMID."""
    from litgraph.search.citations import citation_neighborhood, get_citing_papers, get_references

    if direction in ("cites", "both"):
        console.print(f"[bold]Papers {paper_id} cites:[/bold]")
        _print_results(get_references(paper_id))
    if direction in ("cited-by", "both"):
        console.print(f"[bold]Papers citing {paper_id}:[/bold]")
        _print_results(get_citing_papers(paper_id))
    if depth > 1:
        console.print(f"[bold]{depth}-hop neighborhood:[/bold]")
        _print_results(citation_neighborhood(paper_id, depth=depth))

        

# ------------------------------- SEARCH APP -----------------------------------
@search_app.command("keyword")
def search_keyword(query: str, top_k: int = typer.Option(10, "--top-k")) -> None:
    from litgraph.search.keyword import keyword_search

    _print_results(keyword_search(query, top_k=top_k))


@search_app.command("semantic")
def search_semantic(query: str, top_k: int = typer.Option(10, "--top-k")) -> None:
    from litgraph.search.semantic import semantic_search

    _print_results(semantic_search(query, top_k=top_k))



# ------------------------------- STATS APP ------------------------------------
@stats_app.command("count")
def stats_count() -> None:
    """Total number of papers in the graph."""
    from litgraph.search.stats import paper_count

    console.print(f"Total papers: [bold]{paper_count()}[/bold]")


@stats_app.command("latest")
def stats_latest(n: int = typer.Option(10, "--n", help="Number of papers to show")) -> None:
    """Published dates of the latest N papers."""
    from litgraph.search.stats import latest_papers

    _print_results(latest_papers(limit=n))


@stats_app.command("most-cited")
def stats_most_cited(
    category: str = typer.Option(None, "--category", help="Restrict to a single arXiv category"),
) -> None:
    """The single most cited paper."""
    from litgraph.search.citations import most_cited

    _print_results(most_cited(category=category, limit=1))


@stats_app.command("top-authors")
def stats_top_authors(n: int = typer.Option(10, "--n", help="Number of authors to show")) -> None:
    """The N most prolific authors, by number of papers authored."""
    from litgraph.search.stats import top_authors

    _print_results(top_authors(limit=n))


@stats_app.command("overview")
def stats_overview() -> None:
    """A snapshot of what's in the graph: counts, enrichment coverage, date range."""
    from litgraph.search.stats import overview

    data = overview()

    def pct(part: int, whole: int) -> str:
        return f"{part} ({part / whole:.0%})" if whole else str(part)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Papers", str(data["papers"]))
    table.add_row("     enriched (citation data)", pct(data["enriched"], data["papers"]))
    table.add_row("     embedded (semantic search)", pct(data["embedded"], data["papers"]))
    table.add_row("Citation-graph stub papers", str(data["stubs"]))
    table.add_row("Authors", str(data["authors"]))
    table.add_row("Categories", str(data["categories"]))
    if data["top_category"]:
        table.add_row("     most common", f"{data['top_category']['code']} ({data['top_category']['paper_count']} papers)")
    table.add_row("Edges")
    table.add_row("     authored", str(data["authored_edges"]))
    table.add_row("     in_category", str(data["category_edges"]))
    table.add_row("     cites", str(data["citation_edges"]))
    if data["earliest_published"] and data["latest_published"]:
        table.add_row("Published date range", f"{data['earliest_published']} → {data['latest_published']}")

    console.print(Panel(table, title="litgraph snapshot", expand=False))



if __name__ == "__main__":
    app()
