from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from neo4j import Driver, GraphDatabase

from arxiv_graphdb.config import get_settings


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
    return GraphDatabase.driver(uri, auth=(user, password))


def _session_database() -> str | None:
    """Neo4j has an implicit default database; ArcadeDB (the default backend) doesn't,
    so its Bolt sessions must name one explicitly."""
    settings = get_settings()
    if settings.graph_backend == "neo4j":
        return None
    return settings.arcadedb_database


def run_write(cypher: str, **params: Any) -> list[dict]:
    driver = get_driver()
    with driver.session(database=_session_database()) as session:
        result = session.execute_write(lambda tx: list(tx.run(cypher, **params)))
        return [record.data() for record in result]


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
