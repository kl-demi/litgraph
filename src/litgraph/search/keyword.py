import re

from litgraph.config import get_settings
from litgraph.db import arcadedb_http
from litgraph.db.neo4j_client import run_read

# Characters that Lucene's query parser treats as syntax (field:value, +required, etc.)
# rather than literal text — we want to remove them from titles/abstracts
_LUCENE_SPECIAL_CHARS = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')

_KEYWORD_QUERY = """
CALL db.index.fulltext.queryNodes('paper_fulltext', $search_text) YIELD node, score
WHERE node.is_stub = false
RETURN node.id AS id, node.arxiv_id AS arxiv_id, node.pmid AS pmid, node.title AS title,
       node.abstract AS abstract, node.categories AS categories, score
ORDER BY score DESC
LIMIT $top_k
"""

# ArcadeDB's full-text index is SQL-only (SEARCH_INDEX), which runs over the HTTP API 
# instead of Bolt.
# `is_stub` is filtered here (in SQL, before LIMIT) rather than client-side after the
# fact — ~80% of Paper records are citation-graph stub placeholders with an indexed
# title but no abstract, and their short "documents" can outscore real papers under
# BM25's length normalization, so a post-limit filter could silently return nothing.
_ARCADEDB_KEYWORD_QUERY = """
SELECT id, arxiv_id, pmid, title, abstract, categories, $score AS score FROM Paper
WHERE SEARCH_INDEX('Paper[title,abstract]', :search_text) AND is_stub = false
ORDER BY $score DESC
LIMIT :top_k
"""


def _terms(query: str) -> list[str]:
    """Strip Lucene syntax characters so arbitrary pasted text (a title with a colon
    or hyphen, say) is treated as literal words rather than field/operator syntax."""
    return [t for t in _LUCENE_SPECIAL_CHARS.sub(" ", query).split() if t]


def keyword_search(query: str, top_k: int = 10) -> list[dict]:
    if get_settings().graph_backend == "neo4j":
        return run_read(_KEYWORD_QUERY, search_text=query, top_k=top_k)
    terms = _terms(query)
    if not terms:
        return []
    # AND-match first on multi-word query (e.g. a full paper title)
    # otherwise matches any document containing even one common word like "with",
    # which can match hundreds of thousands of rows and take over a minute to score.
    and_query = " ".join(f"+{t}" for t in terms)
    rows = arcadedb_http.run_query(_ARCADEDB_KEYWORD_QUERY, search_text=and_query, top_k=top_k)
    if not rows:
        # Fall back to OR matching for fuzzy/partial queries that don't share every
        # term with any single document.
        or_query = " ".join(terms)
        rows = arcadedb_http.run_query(_ARCADEDB_KEYWORD_QUERY, search_text=or_query, top_k=top_k)
    return [
        {
            "id": row.get("id"),
            "arxiv_id": row.get("arxiv_id"),
            "pmid": row.get("pmid"),
            "title": row.get("title"),
            "abstract": row.get("abstract"),
            "categories": row.get("categories"),
            "score": row.get("score"),
        }
        for row in rows
    ]
