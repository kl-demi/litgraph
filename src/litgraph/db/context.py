"""Per-context database selection, overriding settings.arcadedb_database.

Set by the dashboard's database dropdown. CLI and cron paths never call
set_database() and keep resolving to ARCADEDB_DATABASE. A ContextVar (not a
module global) so concurrent Streamlit sessions can't clobber each other.
"""

from contextvars import ContextVar

from litgraph.config import get_settings

_database: ContextVar[str | None] = ContextVar("litgraph_database", default=None)


def set_database(name: str | None) -> None:
    """Route subsequent HTTP and Bolt calls in this context to `name`."""
    _database.set(name)


def current_database() -> str:
    return _database.get() or get_settings().arcadedb_database
