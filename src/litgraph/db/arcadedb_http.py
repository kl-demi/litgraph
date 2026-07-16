from typing import Any

import httpx

from litgraph.config import get_settings


def run_command(sql: str, **params: Any) -> list[dict]:
    """Execute a mutating SQL statement (DDL, INSERT, UPDATE) against ArcadeDB."""
    return _post("command", sql, params)


def run_query(sql: str, **params: Any) -> list[dict]:
    """Execute a read-only SQL statement against ArcadeDB."""
    return _post("query", sql, params)


def ensure_ddl(sql: str) -> None:
    """Run a DDL statement, tolerating 'already exists' so schema setup is idempotent.

    ArcadeDB's `IF NOT EXISTS` clause only works on `CREATE VERTEX/EDGE TYPE`, not on
    `CREATE PROPERTY` or `CREATE INDEX` (both raise a parse error) — so idempotency has
    to be done by catching the "already exists" error instead.
    """
    try:
        run_command(sql)
    except httpx.HTTPStatusError as exc:
        if "already exists" not in _error_detail(exc):
            raise


def ensure_database() -> None:
    settings = get_settings()
    try:
        response = httpx.post(
            f"{settings.arcadedb_http_url}/api/v1/server",
            json={"command": f"create database {settings.arcadedb_database}"},
            auth=(settings.arcadedb_user, settings.arcadedb_password),
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if "already exists" not in _error_detail(exc):
            raise


def _post(endpoint: str, sql: str, params: dict) -> list[dict]:
    settings = get_settings()
    url = f"{settings.arcadedb_http_url}/api/v1/{endpoint}/{settings.arcadedb_database}"
    body: dict[str, Any] = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    response = httpx.post(
        url,
        json=body,
        auth=(settings.arcadedb_user, settings.arcadedb_password),
        timeout=settings.arcadedb_http_timeout,
    )
    response.raise_for_status()
    return response.json().get("result", [])


def _error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("detail", "")
    except ValueError:
        return exc.response.text
