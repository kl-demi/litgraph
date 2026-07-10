from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_read
from litgraph.ingest.embeddings import embed_texts

_VECTOR_QUERY = """
CALL db.index.vector.queryNodes('paper_embedding', $top_k, $vector) YIELD node, score
WHERE node.is_stub = false
RETURN node.id AS id, node.arxiv_id AS arxiv_id, node.pmid AS pmid, node.title AS title,
       node.abstract AS abstract, node.categories AS categories, score
ORDER BY score DESC
"""

# ArcadeDB's vector index has no Cypher-callable equivalent to db.index.vector.queryNodes —
# it's SQL-only, so this runs over the HTTP API instead of the Bolt driver.
_ARCADEDB_VECTOR_QUERY = """
SELECT expand(vector.neighbors('Paper[embedding]', :vector, :top_k))
"""


def semantic_search(query: str, top_k: int = 10) -> list[dict]:
    vector = embed_texts([query])[0]
    if get_settings().graph_backend == "neo4j":
        return run_read(_VECTOR_QUERY, top_k=top_k, vector=vector)
    rows = arcadedb_http.run_query(_ARCADEDB_VECTOR_QUERY, vector=vector, top_k=top_k)
    return [
        {
            "id": row.get("id"),
            "arxiv_id": row.get("arxiv_id"),
            "pmid": row.get("pmid"),
            "title": row.get("title"),
            "abstract": row.get("abstract"),
            "categories": row.get("categories"),
            # vector.neighbors returns cosine *distance* (lower = more similar),
            # the inverse of Neo4j's similarity *score* (higher = more similar).
            "score": row.get("distance"),
        }
        for row in rows
        if not row.get("is_stub")
    ]
