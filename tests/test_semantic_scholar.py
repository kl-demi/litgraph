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

    assert len(results) == 1
    result = results[0]
    assert result.paper_id == "2101.00001"
    assert result.s2_paper_id == "s2-1"
    assert result.citation_count == 5
    assert len(result.references) == 1
    assert result.references[0].arxiv_id == "2001.00001"
    assert len(result.citations) == 1
    assert result.citations[0].s2_paper_id == "s2-3"

    kwargs = mock_post.call_args.kwargs
    assert kwargs["json"]["ids"] == ["ARXIV:2101.00001", "ARXIV:2101.99999"]
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
