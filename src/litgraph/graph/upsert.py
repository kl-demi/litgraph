"""Paper-graph writes: papers, categories, authors, citation stubs/edges, enrichment.

Exports:
    upsert_papers: full Paper nodes plus Category/Author nodes and their edges.
    upsert_paper_stubs / upsert_citation_edges: the citation graph around known papers.
    apply_enrichment: Semantic Scholar citation counts, stubs, and CITES edges.
    set_paper_embeddings: embedding backfill that touches no other Paper field.

Usage: ingest/pipeline.py calls these per batch. Every write is idempotent, and each
returns GraphStats deltas so `stats overview` never has to scan the graph.
"""

from litgraph.db.neo4j_client import run_write
from litgraph.graph.writer import CreateMissing, upsert_edges, upsert_nodes
from litgraph.models import PAPER_IDENTIFIERS, CitationStub, EnrichmentResult, Paper, identifier_columns

# `SET paper.arxiv_id = p.arxiv_id, ...`, one line per registered identifier namespace, so
# adding a paper source doesn't mean editing the query text.
_IDENTIFIER_SET_CLAUSE = ",\n    ".join(f"paper.{ns.column} = p.{ns.column}" for ns in PAPER_IDENTIFIERS)

# Returns GraphStats deltas computed via `ON CREATE SET x._is_new = true` sentinels
# (removed before returning), so re-ingesting the same papers doesn't inflate counters.
_UPSERT_PAPERS = f"""
UNWIND $papers AS p
MERGE (paper:Paper {{id: p.id}})
ON CREATE SET paper._is_new = true
WITH paper, p,
     coalesce(paper._is_new, false) AS is_new,
     coalesce(paper.is_stub, false) AS was_stub,
     paper.embedding IS NOT NULL AS was_embedded
REMOVE paper._is_new
SET {_IDENTIFIER_SET_CLAUSE},
    paper.title = p.title,
    paper.abstract = p.abstract,
    paper.categories = p.categories,
    paper.primary_category = p.primary_category,
    paper.published_date = p.published_date,
    paper.updated_date = p.updated_date,
    paper.doi = p.doi,
    paper.journal_ref = p.journal_ref,
    paper.comments = p.comments,
    paper.source = p.source,
    paper.is_stub = false,
    paper.embedding = p.embedding,
    paper.fetched_at = p.fetched_at,
    paper.embedded_at = p.embedded_at
WITH is_new, was_stub, was_embedded, p.embedding IS NOT NULL AS is_embedded, p.published_date AS pub_date
RETURN count(CASE WHEN is_new OR was_stub THEN 1 END) AS new_papers,
       count(CASE WHEN was_stub THEN 1 END) AS upgraded_stubs,
       sum(CASE WHEN is_embedded AND NOT was_embedded THEN 1
                WHEN was_embedded AND NOT is_embedded THEN -1
                ELSE 0 END) AS embedded_delta,
       min(pub_date) AS batch_min_date,
       max(pub_date) AS batch_max_date
"""

_APPLY_PAPER_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.papers = coalesce(g.papers, 0) + $new_papers,
    g.stubs = coalesce(g.stubs, 0) - $upgraded_stubs,
    g.embedded = coalesce(g.embedded, 0) + $embedded_delta,
    g.earliest_published = CASE
        WHEN $batch_min_date IS NULL THEN g.earliest_published
        WHEN g.earliest_published IS NULL OR $batch_min_date < g.earliest_published THEN $batch_min_date
        ELSE g.earliest_published END,
    g.latest_published = CASE
        WHEN $batch_max_date IS NULL THEN g.latest_published
        WHEN g.latest_published IS NULL OR $batch_max_date > g.latest_published THEN $batch_max_date
        ELSE g.latest_published END
"""

# Takes a pre-flattened top-level `$categories` list ({paper_id, code, vocabulary, name})
# because ArcadeDB's Cypher layer mishandles both nested list params and MERGE inside
# FOREACH (the latter creates blank orphan vertices).
# `vocabulary`/`name` are derived from the code and written by nothing else, so SETting
# them unconditionally is safe and self-heals nodes from older ingestions.
_UPSERT_CATEGORIES = """
UNWIND $categories AS cat
MATCH (paper:Paper {id: cat.paper_id})
MERGE (c:Category {code: cat.code})
ON CREATE SET c._is_new = true
WITH paper, c, cat, coalesce(c._is_new, false) AS new_category
REMOVE c._is_new
SET c.vocabulary = cat.vocabulary, c.name = cat.name
MERGE (paper)-[edge:IN_CATEGORY]->(c)
ON CREATE SET edge._is_new = true
WITH c, new_category, coalesce(edge._is_new, false) AS new_edge
REMOVE edge._is_new
SET c.paper_count = coalesce(c.paper_count, 0) + CASE WHEN new_edge THEN 1 ELSE 0 END
RETURN sum(CASE WHEN new_category THEN 1 ELSE 0 END) AS new_categories,
       sum(CASE WHEN new_edge THEN 1 ELSE 0 END) AS new_edges
"""

_APPLY_CATEGORY_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.categories = coalesce(g.categories, 0) + $new_categories,
    g.category_edges = coalesce(g.category_edges, 0) + $new_edges
"""

_UPSERT_AUTHORS = """
UNWIND $papers AS p
UNWIND p.authors AS authorName
MATCH (paper:Paper {id: p.id})
MERGE (a:Author {name: authorName})
ON CREATE SET a._is_new = true
WITH paper, a, coalesce(a._is_new, false) AS new_author
REMOVE a._is_new
MERGE (a)-[edge:AUTHORED]->(paper)
ON CREATE SET edge._is_new = true
WITH new_author, coalesce(edge._is_new, false) AS new_edge
REMOVE edge._is_new
RETURN sum(CASE WHEN new_author THEN 1 ELSE 0 END) AS new_authors,
       sum(CASE WHEN new_edge THEN 1 ELSE 0 END) AS new_edges
"""

_APPLY_AUTHOR_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.authors = coalesce(g.authors, 0) + $new_authors,
    g.authored_edges = coalesce(g.authored_edges, 0) + $new_edges
"""

_APPLY_STUB_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.stubs = coalesce(g.stubs, 0) + $new_stubs
"""

_APPLY_CITATION_EDGE_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.citation_edges = coalesce(g.citation_edges, 0) + $new_edges
"""

_UPDATE_ENRICHMENT = """
UNWIND $results AS r
MATCH (paper:Paper {id: r.paper_id})
WITH paper, r, (paper.citation_count IS NULL AND r.citation_count IS NOT NULL) AS newly_enriched
SET paper.s2_paper_id = r.s2_paper_id,
    paper.citation_count = r.citation_count,
    paper.reference_count = r.reference_count,
    paper.influential_citation_count = r.influential_citation_count,
    paper.enriched_at = r.enriched_at
RETURN count(CASE WHEN newly_enriched THEN 1 END) AS newly_enriched_count
"""

_APPLY_ENRICHMENT_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.enriched = coalesce(g.enriched, 0) + $newly_enriched_count
"""

# Touches only embedding/embedded_at -- backfilling via upsert_papers() would blank every
# Paper field it can't reconstruct (the bug backfill_authors.py once caused).
_SET_EMBEDDINGS = """
UNWIND $embeddings AS e
MATCH (paper:Paper {id: e.id})
SET paper.embedding = e.embedding, paper.embedded_at = e.embedded_at
"""

_APPLY_EMBEDDING_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.embedded = coalesce(g.embedded, 0) + $newly_embedded_count
"""


def _paper_params(paper: Paper) -> dict:
    return {
        "id": paper.id,
        **dict(identifier_columns(paper.identifiers)),
        "title": paper.title,
        "abstract": paper.abstract,
        # Flat namespaced codes, so `$category IN p.categories` works; vocabulary and
        # display name live on the Category node.
        "categories": paper.category_codes(),
        "primary_category": paper.primary_category,
        "published_date": paper.published_date.isoformat() if paper.published_date else None,
        "updated_date": paper.updated_date.isoformat() if paper.updated_date else None,
        "doi": paper.doi,
        "journal_ref": paper.journal_ref,
        "comments": paper.comments,
        # .value: the Bolt driver would otherwise send an enum object, not the string.
        "source": paper.source.value,
        "embedding": paper.embedding,
        "fetched_at": paper.fetched_at.isoformat() if paper.fetched_at else None,
        "embedded_at": paper.embedded_at.isoformat() if paper.embedded_at else None,
        "authors": paper.authors,
    }


def _category_params(papers: list[Paper]) -> list[dict]:
    """One flat row per (paper, category) pair, deduped -- a paper listing the same code
    twice would otherwise inflate Category.paper_count on first write."""
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for paper in papers:
        for category in paper.categories:
            key = (paper.id, category.code)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "paper_id": paper.id,
                    "code": category.code,
                    "vocabulary": category.vocabulary.value,
                    "name": category.name,
                }
            )
    return rows


def upsert_papers(papers: list[Paper]) -> None:
    if not papers:
        return
    params = [_paper_params(p) for p in papers]

    paper_delta = run_write(_UPSERT_PAPERS, papers=params)[0]
    run_write(_APPLY_PAPER_STATS, **paper_delta)

    category_rows = _category_params(papers)
    if category_rows:
        category_delta = run_write(_UPSERT_CATEGORIES, categories=category_rows)[0]
        run_write(_APPLY_CATEGORY_STATS, **category_delta)

    author_delta = run_write(_UPSERT_AUTHORS, papers=params)[0]
    run_write(_APPLY_AUTHOR_STATS, **author_delta)


def upsert_paper_stubs(stubs: list[CitationStub]) -> None:
    """Upsert minimal Paper nodes for citation endpoints not yet ingested.

    Never updates on match: a stub target that is already a fully-ingested Paper would
    otherwise have its fields blanked and `is_stub` flipped back to true.
    """
    if not stubs:
        return
    deduped: dict[str, CitationStub] = {s.id: s for s in stubs}
    rows = [
        {"id": s.id, "title": s.title, "is_stub": True, **dict(identifier_columns(s.identifiers))}
        for s in deduped.values()
    ]
    new_stubs = upsert_nodes("Paper", rows, update_existing=False)
    run_write(_APPLY_STUB_STATS, new_stubs=new_stubs)


def upsert_citation_edges(edges: list[tuple[str, str]]) -> None:
    """Upsert CITES edges between papers already in the graph.

    Neither endpoint is bootstrapped -- `upsert_paper_stubs` runs first and is what creates
    a missing one, with its title and identifiers attached.
    """
    if not edges:
        return
    rows = [{"src": citing, "dst": cited} for citing, cited in {(c, t) for c, t in edges}]
    new_edges = upsert_edges("CITES", rows, create_missing=CreateMissing.NONE, update_existing=False)
    run_write(_APPLY_CITATION_EDGE_STATS, new_edges=new_edges)


def apply_enrichment(results: list[EnrichmentResult]) -> None:
    """Write citation counts, CITES edges, and stub nodes for a batch of enrichment results."""
    if not results:
        return

    stubs: list[CitationStub] = []
    edges: list[tuple[str, str]] = []
    for r in results:
        for ref in r.references:
            stubs.append(ref)
            edges.append((r.paper_id, ref.id))
        for citer in r.citations:
            stubs.append(citer)
            edges.append((citer.id, r.paper_id))

    upsert_paper_stubs(stubs)
    upsert_citation_edges(edges)
    enrichment_delta = run_write(
        _UPDATE_ENRICHMENT,
        results=[
            {
                "paper_id": r.paper_id,
                "s2_paper_id": r.s2_paper_id,
                "citation_count": r.citation_count,
                "reference_count": r.reference_count,
                "influential_citation_count": r.influential_citation_count,
                "enriched_at": r.enriched_at.isoformat() if r.enriched_at else None,
            }
            for r in results
        ],
    )[0]
    run_write(_APPLY_ENRICHMENT_STATS, **enrichment_delta)


def set_paper_embeddings(embeddings: list[tuple[str, list[float]]], embedded_at) -> None:
    """Write embeddings for already-ingested papers (e.g. backfilling ones upserted
    during an embedding-service outage). ``embeddings`` is a list of (paper_id, vector)."""
    if not embeddings:
        return
    run_write(
        _SET_EMBEDDINGS,
        embeddings=[
            {"id": paper_id, "embedding": vector, "embedded_at": embedded_at.isoformat()}
            for paper_id, vector in embeddings
        ],
    )
    run_write(_APPLY_EMBEDDING_STATS, newly_embedded_count=len(embeddings))
