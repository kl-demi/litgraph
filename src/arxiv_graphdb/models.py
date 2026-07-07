from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class Paper(BaseModel):
    """A single arXiv paper, normalized from either the arXiv API or the Kaggle snapshot."""

    arxiv_id: str | None = None
    s2_paper_id: str | None = None

    title: str
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    primary_category: str | None = None

    published_date: date | None = None
    updated_date: date | None = None
    doi: str | None = None
    journal_ref: str | None = None
    comments: str | None = None

    source: str = "arxiv"  # "arxiv" | "kaggle"

    embedding: list[float] | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    influential_citation_count: int | None = None

    fetched_at: datetime | None = None
    enriched_at: datetime | None = None
    embedded_at: datetime | None = None

    @property
    def id(self) -> str:
        """MERGE key: prefer arxiv_id, fall back to a namespaced Semantic Scholar id."""
        if self.arxiv_id:
            return self.arxiv_id
        if self.s2_paper_id:
            return f"s2:{self.s2_paper_id}"
        raise ValueError("Paper needs at least one of arxiv_id or s2_paper_id")

    @model_validator(mode="after")
    def _require_identifier(self) -> "Paper":
        if not self.arxiv_id and not self.s2_paper_id:
            raise ValueError("Paper needs at least one of arxiv_id or s2_paper_id")
        return self


class CitationStub(BaseModel):
    """A minimal reference to a paper on the other end of a CITES edge.

    May or may not already exist as a full Paper node in the graph -- if not, it is
    upserted as a stub (is_stub=true) and filled in later if that paper is fully ingested.
    """

    arxiv_id: str | None = None
    s2_paper_id: str | None = None
    title: str | None = None

    @property
    def id(self) -> str:
        if self.arxiv_id:
            return self.arxiv_id
        if self.s2_paper_id:
            return f"s2:{self.s2_paper_id}"
        raise ValueError("CitationStub needs at least one of arxiv_id or s2_paper_id")


class EnrichmentResult(BaseModel):
    """Semantic Scholar enrichment output for one paper."""

    arxiv_id: str
    s2_paper_id: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    influential_citation_count: int | None = None
    references: list[CitationStub] = Field(default_factory=list)  # papers this one cites
    citations: list[CitationStub] = Field(default_factory=list)  # papers that cite this one
    enriched_at: datetime | None = None
