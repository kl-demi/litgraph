"""Per-database search hints: what to put in the search box before anyone types.

Derived from the graph rather than configured, so a newly ingested corpus arrives with
sensible entry points instead of another database's. A curated override exists for the
one thing measurement cannot supply -- an example sentence in natural English.
"""

from litgraph.db.neo4j_client import run_read
from litgraph.search.entities import searchable_types

# Editorial only. Everything else is measured; a plausible example query is the one
# thing the graph cannot write for you.
_PLACEHOLDERS = {
    "rice": "Describe what you are looking for — e.g. how rice tolerates drought",
    "human": "Describe what you are looking for — e.g. what drives acute myeloid leukemia",
}
_DEFAULT_PLACEHOLDER = "Describe what you are looking for — e.g. a mechanism, a disease, a phenotype"

# The order suggestions are drawn in, so the chips spread across kinds of thing rather
# than showing four genes. Types absent from a database are skipped.
_SUGGESTION_ORDER = ("Disease", "Trait", "Gene", "Pathway", "Compound", "Organism")

_TOP_BY_MENTIONS = """
MATCH (p:Paper)-[:MENTIONS]->(n:{label})
WHERE n.name IS NOT NULL
RETURN n.name AS name, count(p) AS papers
ORDER BY papers DESC LIMIT $limit
"""

# Counting incoming relationships rather than size((n)<--()): ArcadeDB's Cypher rejects
# a pattern expression there outright.
_TOP_BY_DEGREE = """
MATCH (n:{label})<-[r]-()
WHERE n.name IS NOT NULL
RETURN n.name AS name, count(r) AS papers
ORDER BY papers DESC LIMIT $limit
"""


def placeholder(db: str) -> str:
    return _PLACEHOLDERS.get(db, _DEFAULT_PLACEHOLDER)


def suggestions(db: str, count: int = 4) -> tuple[str, ...]:
    """Suggestion chips for this database, from the cache if one has been built.

    Falls back to measuring them, so a database that has never had `litgraph stats
    rebuild` run against it still gets chips -- just slowly.
    """
    from litgraph.search.stats import search_hints  # circular at module level

    cached = search_hints()
    return tuple(cached[:count]) if cached else measure_suggestions(count)


def measure_suggestions(count: int = 4) -> tuple[str, ...]:
    """The best-connected entity of each kind, most-connected kind first.

    Ranked by how many papers mention the entity, so a suggestion is guaranteed to
    return results. Types reached only by non-MENTIONS edges (Pathway, Trait) fall back
    to total degree. Scans a slice of MENTIONS per type; prefer the cached
    `suggestions`.
    """
    available = searchable_types()
    picked: list[str] = []
    for label in _SUGGESTION_ORDER:
        if label not in available or len(picked) >= count:
            continue
        for query in (_TOP_BY_MENTIONS, _TOP_BY_DEGREE):
            try:
                rows = run_read(query.format(label=label), limit=3)
            except Exception:  # a type the backend can't traverse this way
                continue
            names = [r["name"] for r in rows if r.get("name") and r.get("papers")]
            if names:
                picked.append(names[0])
                break
    return tuple(picked)
