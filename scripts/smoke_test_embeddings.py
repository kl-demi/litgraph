"""Standalone check that the SPECTER2 adapter loads and produces sane embeddings.
No DB connection required. Run after `uv sync` to verify the model download and
adapter loading work in a new environment (e.g. a fresh AWS instance) before
running the real ingestion/backfill pipeline.
"""

from litgraph.ingest.embeddings import embed_texts, paper_embedding_text

_SAMPLE_PAPERS = [
    ("Attention Is All You Need", "We propose the Transformer, a model architecture "
     "based solely on attention mechanisms, dispensing with recurrence and "
     "convolutions entirely."),
    ("Deep contextualized word representations", "We introduce a new type of deep "
     "contextualized word representation that models both complex characteristics "
     "of word use and how these uses vary across linguistic contexts."),
    ("Photosynthesis in C4 plants", "We review the biochemical pathway of carbon "
     "fixation in C4 plants and its adaptive advantages under high light and "
     "temperature conditions."),
]


def main() -> None:
    texts = [paper_embedding_text(title, abstract) for title, abstract in _SAMPLE_PAPERS]

    print(f"Encoding {len(texts)} sample papers...")
    vectors = embed_texts(texts)

    for (title, _), vector in zip(_SAMPLE_PAPERS, vectors):
        print(f"  {title!r}: dim={len(vector)}, first 5 values={vector[:5]}")

    dims = {len(v) for v in vectors}
    assert dims == {768}, f"expected all vectors to be 768-dim, got {dims}"

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        return dot / (norm_a * norm_b)

    sim_related = cosine(vectors[0], vectors[1])  # both NLP/transformer papers
    sim_unrelated = cosine(vectors[0], vectors[2])  # NLP vs plant biology

    print(f"\ncosine(transformer paper, ELMo paper)      = {sim_related:.4f}")
    print(f"cosine(transformer paper, photosynthesis)  = {sim_unrelated:.4f}")

    assert sim_related > sim_unrelated, (
        "expected the two NLP papers to be more similar to each other than to the "
        "unrelated biology paper - embeddings may not be working as expected"
    )

    print("\nOK: SPECTER2 adapter loaded, embeddings are 768-dim, and relative "
          "similarity looks sane.")


if __name__ == "__main__":
    main()
