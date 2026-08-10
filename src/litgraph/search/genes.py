from litgraph.db.neo4j_client import run_read

_SEARCH_GENES = """
MATCH (g:Gene) WHERE toLower(g.name) CONTAINS toLower($name_substr)
RETURN g.gene_id AS gene_id, g.name AS name
LIMIT $limit
"""

_PAPERS_MENTIONING_GENE = """
MATCH (g:Gene {gene_id: $gene_id})
MATCH (p:Paper)-[m:MENTIONS]->(g)
RETURN p.id AS id, p.arxiv_id AS arxiv_id, p.pmid AS pmid, p.title AS title, m.source AS source
LIMIT $limit
"""

_PATHWAYS_FOR_GENE = """
MATCH (g:Gene {gene_id: $gene_id})
MATCH (g)-[r:PARTICIPATES_IN]->(pw:Pathway)
RETURN pw.pathway_id AS pathway_id, pw.name AS name, pw.source_db AS source_db,
       r.evidence_code AS evidence_code
LIMIT $limit
"""

_CO_MENTIONED_GENES = """
MATCH (g:Gene {gene_id: $gene_id})
MATCH (p:Paper)-[:MENTIONS]->(g)
MATCH (p)-[:MENTIONS]->(other:Gene) WHERE other.gene_id <> $gene_id
RETURN other.gene_id AS gene_id, other.name AS name, count(DISTINCT p) AS shared_papers
ORDER BY shared_papers DESC
LIMIT $limit
"""


def search_genes(name_substr: str, limit: int = 20) -> list[dict]:
    """Genes whose name contains ``name_substr`` (case-insensitive)."""
    return run_read(_SEARCH_GENES, name_substr=name_substr, limit=limit)


def papers_mentioning_gene(gene_id: str, limit: int = 50) -> list[dict]:
    """Papers that mention the gene identified by ``gene_id`` (e.g. ``ncbigene:7157``)."""
    return run_read(_PAPERS_MENTIONING_GENE, gene_id=gene_id, limit=limit)


def pathways_for_gene(gene_id: str, limit: int = 50) -> list[dict]:
    """Pathways the gene identified by ``gene_id`` participates in."""
    return run_read(_PATHWAYS_FOR_GENE, gene_id=gene_id, limit=limit)


def co_mentioned_genes(gene_id: str, limit: int = 20) -> list[dict]:
    """Other genes most often mentioned in the same papers as ``gene_id``, most-shared first."""
    return run_read(_CO_MENTIONED_GENES, gene_id=gene_id, limit=limit)
