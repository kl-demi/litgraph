from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from neo4j import Driver, GraphDatabase

from arxiv_graphdb.config import get_settings


@lru_cache
def get_driver() -> Driver:
    settings = get_settings()
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


def run_write(cypher: str, **params: Any) -> list[dict]:
    driver = get_driver()
    with driver.session() as session:
        result = session.execute_write(lambda tx: list(tx.run(cypher, **params)))
        return [record.data() for record in result]


def run_read(cypher: str, **params: Any) -> list[dict]:
    driver = get_driver()
    with driver.session() as session:
        result = session.execute_read(lambda tx: list(tx.run(cypher, **params)))
        return [record.data() for record in result]


def close_driver() -> None:
    if get_driver.cache_info().currsize:
        get_driver().close()
        get_driver.cache_clear()


def chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
