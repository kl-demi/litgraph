import numpy as np

from arxiv_graphdb.ingest import embeddings


class FakeModel:
    def encode(self, texts, **kwargs):
        return np.array([[float(len(t)), 0.0, 1.0] for t in texts])


def test_embed_texts_returns_list_of_lists(mocker):
    embeddings._get_model.cache_clear()
    mocker.patch.object(embeddings, "_get_model", return_value=FakeModel())

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
