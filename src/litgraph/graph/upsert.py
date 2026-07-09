from litgraph.db.neo4j_client import run_write
from litgraph.models import CitationStub, EnrichmentResult, Paper

_UPSERT_PAPERS = """
UNWIND $papers AS p
MERGE (paper:Paper {id: p.id})
SET paper.arxiv_id = p.arxiv_id,
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
WITH paper, p
FOREACH (cat IN p.categories |
    MERGE (c:Category {code: cat})
    MERGE (paper)-[:IN_CATEGORY]->(c)
)
WITH paper, p
FOREACH (authorName IN p.authors |
    MERGE (a:Author {name: authorName})
    MERGE (a)-[:AUTHORED]->(paper)
)
"""

_UPSERT_STUBS = """
UNWIND $stubs AS s
MERGE (stub:Paper {id: s.id})
ON CREATE SET stub.is_stub = true,
              stub.title = s.title,
              stub.arxiv_id = s.arxiv_id,
              stub.s2_paper_id = s.s2_paper_id
"""

_UPSERT_CITATION_EDGES = """
UNWIND $edges AS e
MATCH (citing:Paper {id: e.citing_id})
MATCH (cited:Paper {id: e.cited_id})
MERGE (citing)-[:CITES]->(cited)
"""

_UPDATE_ENRICHMENT = """
UNWIND $results AS r
MATCH (paper:Paper {id: r.arxiv_id})
SET paper.s2_paper_id = r.s2_paper_id,
    paper.citation_count = r.citation_count,
    paper.reference_count = r.reference_count,
    paper.influential_citation_count = r.influential_citation_count,
    paper.enriched_at = r.enriched_at
"""


def _paper_params(paper: Paper) -> dict:
    return {
        "id": paper.id,
        "arxiv_id": paper.arxiv_id,
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
        "fetched_at": paper.fetched_at,
        "embedded_at": paper.embedded_at,
        "authors": paper.authors,
    }


def upsert_papers(papers: list[Paper]) -> None:
    if not papers:
        return
    run_write(_UPSERT_PAPERS, papers=[_paper_params(p) for p in papers])


def upsert_paper_stubs(stubs: list[CitationStub]) -> None:
    if not stubs:
        return
    deduped: dict[str, CitationStub] = {s.id: s for s in stubs}
    run_write(
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
    )


def upsert_citation_edges(edges: list[tuple[str, str]]) -> None:
    if not edges:
        return
    deduped = {(c, t) for c, t in edges}
    run_write(
        _UPSERT_CITATION_EDGES,
        edges=[{"citing_id": c, "cited_id": t} for c, t in deduped],
    )


def apply_enrichment(results: list[EnrichmentResult]) -> None:
    """Write citation counts, CITES edges, and stub nodes for a batch of enrichment results."""
    if not results:
        return

    stubs: list[CitationStub] = []
    edges: list[tuple[str, str]] = []
    for r in results:
        for ref in r.references:
            stubs.append(ref)
            edges.append((r.arxiv_id, ref.id))
        for citer in r.citations:
            stubs.append(citer)
            edges.append((citer.id, r.arxiv_id))

    upsert_paper_stubs(stubs)
    upsert_citation_edges(edges)
    run_write(
        _UPDATE_ENRICHMENT,
        results=[
            {
                "arxiv_id": r.arxiv_id,
                "s2_paper_id": r.s2_paper_id,
                "citation_count": r.citation_count,
                "reference_count": r.reference_count,
                "influential_citation_count": r.influential_citation_count,
                "enriched_at": r.enriched_at,
            }
            for r in results
        ],
    )
