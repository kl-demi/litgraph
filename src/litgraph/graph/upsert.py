from litgraph.db.neo4j_client import run_write
from litgraph.models import CitationStub, EnrichmentResult, Paper

# Each upsert also returns the deltas needed to keep the GraphStats singleton (see
# _apply_paper_stats etc.) in sync, so `stats overview` never has to full-scan the
# graph. Deltas are computed by comparing a property's value before/after this same
# write, via `MERGE ... ON CREATE SET x._is_new = true` sentinels (removed before
# returning) — this is idempotent under re-ingestion of already-upserted papers,
# unlike a naive "+= len(batch)" counter would be.
_UPSERT_PAPERS = """
UNWIND $papers AS p
MERGE (paper:Paper {id: p.id})
ON CREATE SET paper._is_new = true
WITH paper, p,
     coalesce(paper._is_new, false) AS is_new,
     coalesce(paper.is_stub, false) AS was_stub,
     paper.embedding IS NOT NULL AS was_embedded
REMOVE paper._is_new
SET paper.arxiv_id = p.arxiv_id,
    paper.pmid = p.pmid,
    paper.s2_paper_id = p.s2_paper_id,
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

# Separate top-level UNWIND $papers per relationship type, rather than nesting a
# FOREACH inside the paper-upsert query: ArcadeDB's Bolt/Cypher layer doesn't honor
# MERGE's match-or-create semantics for a pattern variable bound inside a FOREACH —
# `MERGE (a:Author {name: x}) MERGE (a)-[:AUTHORED]->(paper)` inside FOREACH always
# creates a fresh blank vertex for `a` instead of reusing the matched/created Author,
# so edges ended up pointing at untyped, propertyless orphan vertices.
_UPSERT_CATEGORIES = """
UNWIND $papers AS p
UNWIND p.categories AS cat
MATCH (paper:Paper {id: p.id})
MERGE (c:Category {code: cat})
ON CREATE SET c._is_new = true
WITH paper, c, coalesce(c._is_new, false) AS new_category
REMOVE c._is_new
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

_UPSERT_STUBS = """
UNWIND $stubs AS s
MERGE (stub:Paper {id: s.id})
ON CREATE SET stub.is_stub = true,
              stub.title = s.title,
              stub.arxiv_id = s.arxiv_id,
              stub.pmid = s.pmid,
              stub.s2_paper_id = s.s2_paper_id,
              stub._is_new = true
WITH stub, coalesce(stub._is_new, false) AS is_new
REMOVE stub._is_new
RETURN count(CASE WHEN is_new THEN 1 END) AS new_stubs
"""

_APPLY_STUB_STATS = """
MERGE (g:GraphStats {id: 'singleton'})
SET g.stubs = coalesce(g.stubs, 0) + $new_stubs
"""

_UPSERT_CITATION_EDGES = """
UNWIND $edges AS e
MATCH (citing:Paper {id: e.citing_id})
MATCH (cited:Paper {id: e.cited_id})
MERGE (citing)-[edge:CITES]->(cited)
ON CREATE SET edge._is_new = true
WITH edge, coalesce(edge._is_new, false) AS is_new
REMOVE edge._is_new
RETURN count(CASE WHEN is_new THEN 1 END) AS new_edges
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


def _paper_params(paper: Paper) -> dict:
    return {
        "id": paper.id,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "s2_paper_id": paper.s2_paper_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "categories": paper.categories,
        "primary_category": paper.primary_category,
        "published_date": paper.published_date.isoformat() if paper.published_date else None,
        "updated_date": paper.updated_date.isoformat() if paper.updated_date else None,
        "doi": paper.doi,
        "journal_ref": paper.journal_ref,
        "comments": paper.comments,
        "source": paper.source,
        "embedding": paper.embedding,
        "fetched_at": paper.fetched_at.isoformat() if paper.fetched_at else None,
        "embedded_at": paper.embedded_at.isoformat() if paper.embedded_at else None,
        "authors": paper.authors,
    }


def upsert_papers(papers: list[Paper]) -> None:
    if not papers:
        return
    params = [_paper_params(p) for p in papers]

    paper_delta = run_write(_UPSERT_PAPERS, papers=params)[0]
    run_write(_APPLY_PAPER_STATS, **paper_delta)

    category_delta = run_write(_UPSERT_CATEGORIES, papers=params)[0]
    run_write(_APPLY_CATEGORY_STATS, **category_delta)

    author_delta = run_write(_UPSERT_AUTHORS, papers=params)[0]
    run_write(_APPLY_AUTHOR_STATS, **author_delta)


def upsert_paper_stubs(stubs: list[CitationStub]) -> None:
    if not stubs:
        return
    deduped: dict[str, CitationStub] = {s.id: s for s in stubs}
    stub_delta = run_write(
        _UPSERT_STUBS,
        stubs=[
            {
                "id": s.id,
                "title": s.title,
                "arxiv_id": s.arxiv_id,
                "s2_paper_id": s.s2_paper_id,
            }
            for s in deduped.values()
        ],
    )[0]
    run_write(_APPLY_STUB_STATS, **stub_delta)


def upsert_citation_edges(edges: list[tuple[str, str]]) -> None:
    if not edges:
        return
    deduped = {(c, t) for c, t in edges}
    edge_delta = run_write(
        _UPSERT_CITATION_EDGES,
        edges=[{"citing_id": c, "cited_id": t} for c, t in deduped],
    )[0]
    run_write(_APPLY_CITATION_EDGE_STATS, **edge_delta)


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
