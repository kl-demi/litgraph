from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ClientError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from litgraph.config import get_settings

# ArcadeDB's Bolt plugin is a community reimplementation of the protocol, not Neo4j
# itself, and intermittently loses track of a transaction between its final query and
# the driver's commit -- surfacing as this ClientError even though nothing is actually
# wrong with the query or data (retrying the same transaction immediately succeeds).
_TRANSACTION_NOT_FOUND = "Neo.ClientError.Transaction.TransactionNotFound"


def _is_retryable_bolt_error(exc: BaseException) -> bool:
    return isinstance(exc, ClientError) and exc.code == _TRANSACTION_NOT_FOUND


@lru_cache
def get_driver() -> Driver:
    settings = get_settings()
    if settings.graph_backend == "neo4j":
        uri, user, password = settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    else:
        uri, user, password = (
            settings.arcadedb_uri,
            settings.arcadedb_user,
            settings.arcadedb_password,
        )
    # Ingestion commands (enrich, in particular) can go tens of seconds to a couple
    # minutes between DB calls while paced/rate-limited by an external API -- long
    # enough for a pooled Bolt connection to go stale and get silently dropped.
    # Without this, the driver hands out the dead connection and callers see a
    # confusing TransactionNotFound/"defunct connection" error instead of a clean
    # reconnect.
    return GraphDatabase.driver(uri, auth=(user, password), liveness_check_timeout=30)


def _session_database() -> str | None:
    """Neo4j has an implicit default database; ArcadeDB (the default backend) doesn't,
    so its Bolt sessions must name one explicitly."""
    settings = get_settings()
    if settings.graph_backend == "neo4j":
        return None
    return settings.arcadedb_database


@retry(
    retry=retry_if_exception(_is_retryable_bolt_error),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    stop=stop_after_attempt(5),
    reraise=True,
)
def run_write(cypher: str, **params: Any) -> list[dict]:
    driver = get_driver()
    with driver.session(database=_session_database()) as session:
        result = session.execute_write(lambda tx: list(tx.run(cypher, **params)))
        return [record.data() for record in result]


@retry(
    retry=retry_if_exception(_is_retryable_bolt_error),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    stop=stop_after_attempt(5),
    reraise=True,
)
def run_read(cypher: str, **params: Any) -> list[dict]:
    driver = get_driver()
    with driver.session(database=_session_database()) as session:
        result = session.execute_read(lambda tx: list(tx.run(cypher, **params)))
        return [record.data() for record in result]


def close_driver() -> None:
    if get_driver.cache_info().currsize:
        get_driver().close()
        get_driver.cache_clear()


def chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
