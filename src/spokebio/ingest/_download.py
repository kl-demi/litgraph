"""Shared cached-download-with-retry helper for spokebio's bulk-file sources.

Exports:
    ensure_cached_file(url, path, force, timeout): download `url` to `path` if not
        already cached (or `force=True`), retrying on 5xx/transport errors.

Usage: each source module wraps this with its own URL and path defaults, e.g.

    def ensure_obo_file(path=DEFAULT_OBO_PATH, force=False):
        return ensure_cached_file(GO_OBO_URL, Path(path), force)
"""

from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def ensure_cached_file(url: str, path: Path, force: bool = False, timeout: float = 60.0) -> str:
    if path.exists() and not force:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as response:
        response.raise_for_status()
        with path.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(path)
