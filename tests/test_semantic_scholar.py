import httpx

from litgraph.ingest.semantic_scholar import SemanticScholarClient


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_enrich_maps_references_and_citations(mocker):
    payload = [
        {
            "paperId": "s2-1",
            "externalIds": {"ArXiv": "2101.00001"},
            "citationCount": 5,
            "referenceCount": 2,
            "influentialCitationCount": 1,
            "references": [
                {"paperId": "s2-2", "title": "Referenced Paper", "externalIds": {"ArXiv": "2001.00001"}},
                {"paperId": None, "title": "Unresolved", "externalIds": {}},
            ],
            "citations": [
                {"paperId": "s2-3", "title": "Citing Paper", "externalIds": {}},
            ],
        },
        None,
    ]

    client = SemanticScholarClient()
    mocker.patch.object(client, "_throttle")
    mock_post = mocker.patch.object(
        client._client, "post", return_value=FakeResponse(200, payload)
    )

    results = client.enrich([("2101.00001", "2101.00001"), ("2101.99999", "2101.99999")], id_prefix="ARXIV")

    # Both papers get a result now, even the one S2 doesn't recognize -- otherwise it
    # never gets `enriched_at` stamped and keeps reappearing in every future enrich run.
    assert len(results) == 2
    result = next(r for r in results if r.paper_id == "2101.00001")
    assert result.s2_paper_id == "s2-1"
    assert result.citation_count == 5
    assert len(result.references) == 1
    assert result.references[0].arxiv_id == "2001.00001"
    assert len(result.citations) == 1
    assert result.citations[0].s2_paper_id == "s2-3"

    not_found = next(r for r in results if r.paper_id == "2101.99999")
    assert not_found.s2_paper_id is None
    assert not_found.citation_count is None
    assert not_found.enriched_at is not None

    kwargs = mock_post.call_args.kwargs
    assert kwargs["json"]["ids"] == ["ARXIV:2101.00001", "ARXIV:2101.99999"]
    client.close()


def test_enrich_survives_short_response(mocker):
    # Regression test: S2's batch endpoint can silently omit an id from the response
    # array (not even a null placeholder), instead of keeping every id lined up with a
    # same-index response entry. Reproduces a production crash where a 3-id batch got
    # back only 2 items and zip(..., strict=True) raised ValueError.
    payload = [
        {"paperId": "s2-1", "externalIds": {"ArXiv": "2101.00001"}, "citationCount": 5,
         "referenceCount": 0, "influentialCitationCount": 0, "references": [], "citations": []},
        None,
        # third id's entry is missing entirely -- response is shorter than the request
    ]

    client = SemanticScholarClient()
    mocker.patch.object(client, "_throttle")
    mocker.patch.object(client._client, "post", return_value=FakeResponse(200, payload))

    results = client.enrich(
        [("2101.00001", "2101.00001"), ("2101.99999", "2101.99999"), ("2101.55555", "2101.55555")],
        id_prefix="ARXIV",
    )

    assert len(results) == 3
    found = next(r for r in results if r.paper_id == "2101.00001")
    assert found.s2_paper_id == "s2-1"
    for paper_id in ("2101.99999", "2101.55555"):
        not_found = next(r for r in results if r.paper_id == paper_id)
        assert not_found.s2_paper_id is None
        assert not_found.enriched_at is not None
    client.close()


def test_enrich_retries_on_429(mocker):
    payload = [{"paperId": "s2-1", "externalIds": {"ArXiv": "2101.00001"}, "citationCount": 0,
                "referenceCount": 0, "influentialCitationCount": 0, "references": [], "citations": []}]

    client = SemanticScholarClient()
    mocker.patch.object(client, "_throttle")
    mocker.patch("time.sleep")
    responses = [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, payload)]
    mocker.patch.object(client._client, "post", side_effect=responses)

    results = client.enrich([("2101.00001", "2101.00001")], id_prefix="ARXIV")
    assert len(results) == 1
    client.close()
