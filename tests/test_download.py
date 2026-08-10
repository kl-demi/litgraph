import httpx

from spokebio.ingest._download import ensure_cached_file


class _FakeStreamResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self._status, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def iter_bytes(self):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def test_skips_download_if_already_cached(tmp_path, mocker):
    path = tmp_path / "file.txt"
    path.write_text("cached content")
    mock_stream = mocker.patch("spokebio.ingest._download.httpx.stream")

    result = ensure_cached_file("https://example.test/file.txt", path)

    assert result == str(path)
    mock_stream.assert_not_called()


def test_downloads_when_missing_and_creates_parent_dirs(tmp_path, mocker):
    path = tmp_path / "subdir" / "file.txt"
    mocker.patch("spokebio.ingest._download.httpx.stream", return_value=_FakeStreamResponse(b"fresh content"))

    result = ensure_cached_file("https://example.test/file.txt", path)

    assert result == str(path)
    assert path.read_text() == "fresh content"


def test_force_redownloads_even_when_cached(tmp_path, mocker):
    path = tmp_path / "file.txt"
    path.write_text("stale content")
    mocker.patch("spokebio.ingest._download.httpx.stream", return_value=_FakeStreamResponse(b"fresh content"))

    ensure_cached_file("https://example.test/file.txt", path, force=True)

    assert path.read_text() == "fresh content"


def test_passes_the_given_timeout_through(tmp_path, mocker):
    path = tmp_path / "file.txt"
    mock_stream = mocker.patch("spokebio.ingest._download.httpx.stream", return_value=_FakeStreamResponse(b"data"))

    ensure_cached_file("https://example.test/file.txt", path, timeout=120.0)

    assert mock_stream.call_args.kwargs["timeout"] == 120.0


def test_retries_on_a_5xx_then_succeeds(tmp_path, mocker):
    mocker.patch("time.sleep")  # skip tenacity's real backoff delay
    mock_stream = mocker.patch(
        "spokebio.ingest._download.httpx.stream",
        side_effect=[_FakeStreamResponse(b"", status=503), _FakeStreamResponse(b"data")],
    )

    result = ensure_cached_file("https://example.test/file.txt", tmp_path / "file.txt")

    assert result == str(tmp_path / "file.txt")
    assert mock_stream.call_count == 2


def test_does_not_retry_on_a_4xx():
    import spokebio.ingest._download as download_module

    exc = httpx.HTTPStatusError(
        "error", request=httpx.Request("GET", "https://example.test"), response=httpx.Response(404)
    )
    assert download_module._is_retryable(exc) is False


def test_retries_on_a_transport_error():
    import spokebio.ingest._download as download_module

    assert download_module._is_retryable(httpx.TransportError("connection reset")) is True
