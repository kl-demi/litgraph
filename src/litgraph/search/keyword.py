from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_read

_KEYWORD_QUERY = """
CALL db.index.fulltext.queryNodes('paper_fulltext', $search_text) YIELD node, score
WHERE node.is_stub = false
RETURN node.id AS id, node.arxiv_id AS arxiv_id, node.title AS title,
       node.abstract AS abstract, node.categories AS categories, score
ORDER BY score DESC
LIMIT $top_k
"""

# ArcadeDB's full-text index is SQL-only (SEARCH_INDEX), no Cypher-callable equivalent
# to db.index.fulltext.queryNodes — this runs over the HTTP API instead of Bolt.
_ARCADEDB_KEYWORD_QUERY = """
SELECT id, arxiv_id, title, abstract, categories, is_stub, $score AS score FROM Paper
WHERE SEARCH_INDEX('Paper[title,abstract]', :search_text)
ORDER BY $score DESC
LIMIT :top_k
"""


def keyword_search(query: str, top_k: int = 10) -> list[dict]:
    if get_settings().graph_backend == "neo4j":
        return run_read(_KEYWORD_QUERY, search_text=query, top_k=top_k)
    rows = arcadedb_http.run_query(_ARCADEDB_KEYWORD_QUERY, search_text=query, top_k=top_k)
    return [
        {
            "id": row.get("id"),
            "arxiv_id": row.get("arxiv_id"),
            "title": row.get("title"),
            "abstract": row.get("abstract"),
            "categories": row.get("categories"),
            "score": row.get("score"),
        }
        for row in rows
        if not row.get("is_stub")
    ]
