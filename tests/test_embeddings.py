import httpx
import numpy as np

from litgraph.ingest import embeddings


class FakeModel:
    def encode(self, texts, **kwargs):
        return np.array([[float(len(t)), 0.0, 1.0] for t in texts])


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://fake/embed")
            raise httpx.HTTPStatusError("error", request=request, response=self)


def test_embed_remote_retries_on_502_then_succeeds(mocker):
    mocker.patch("time.sleep")
    responses = [FakeResponse(502), FakeResponse(200, {"vectors": [[1.0, 2.0]]})]
    mock_post = mocker.patch("httpx.post", side_effect=responses)

    vectors = embeddings._embed_remote(["some text"], "http://fake-embedding-service")

    assert vectors == [[1.0, 2.0]]
    assert mock_post.call_count == 2


def test_embed_remote_gives_up_after_persistent_502(mocker):
    mocker.patch("time.sleep")
    mocker.patch("httpx.post", return_value=FakeResponse(502))

    try:
        embeddings._embed_remote(["some text"], "http://fake-embedding-service")
        raised = False
    except httpx.HTTPStatusError:
        raised = True

    assert raised


def test_embed_texts_returns_list_of_lists(mocker):
    embeddings._get_model.cache_clear()
    mocker.patch.object(embeddings, "_get_model", return_value=FakeModel())
    # Force the local-model path regardless of the real environment's config -- when
    # EMBEDDING_SERVICE_URL is set (e.g. this dev box's RunPod GPU server, see .env),
    # embed_texts() takes the _embed_remote() branch instead and never calls the
    # mocked _get_model() at all, silently hitting the real remote service.
    mocker.patch.object(embeddings, "get_settings", return_value=mocker.Mock(embedding_service_url=None))

    vectors = embeddings.embed_texts(["hello", "a longer string"])

    assert len(vectors) == 2
    assert all(isinstance(v, list) for v in vectors)
    assert vectors[0][0] == float(len("hello"))


def test_embed_texts_empty_input_short_circuits():
    assert embeddings.embed_texts([]) == []


def test_paper_embedding_text_joins_title_and_abstract():
    text = embeddings.paper_embedding_text("  Title  ", "  Abstract text  ")
    assert text == "Title\nAbstract text"

# def test_embed_texts():
#     embeddings._get_model.cache_clear()
#     vector1 = embeddings.embed_texts(["", ""])
#     vector2 = embeddings.embed_texts(["", ""])
#     assert vector1 == vector2
