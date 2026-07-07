from arxiv_graphdb.db.neo4j_client import run_read

_KEYWORD_QUERY = """
CALL db.index.fulltext.queryNodes('paper_fulltext', $search_text) YIELD node, score
WHERE node.is_stub = false
RETURN node.id AS id, node.arxiv_id AS arxiv_id, node.title AS title,
       node.abstract AS abstract, node.categories AS categories, score
ORDER BY score DESC
LIMIT $top_k
"""


def keyword_search(query: str, top_k: int = 10) -> list[dict]:
    return run_read(_KEYWORD_QUERY, search_text=query, top_k=top_k)
