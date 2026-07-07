from arxiv_graphdb.db.neo4j_client import run_read
from arxiv_graphdb.ingest.embeddings import embed_texts

_VECTOR_QUERY = """
CALL db.index.vector.queryNodes('paper_embedding', $top_k, $vector) YIELD node, score
WHERE node.is_stub = false
RETURN node.id AS id, node.arxiv_id AS arxiv_id, node.title AS title,
       node.abstract AS abstract, node.categories AS categories, score
ORDER BY score DESC
"""


def semantic_search(query: str, top_k: int = 10) -> list[dict]:
    vector = embed_texts([query])[0]
    return run_read(_VECTOR_QUERY, top_k=top_k, vector=vector)
