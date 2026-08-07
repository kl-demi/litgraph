"""Data models for the paper side of the graph.

Exports:
    Paper / CitationStub / EnrichmentResult: the ingestion payloads.
    Source: enum of ingestion paths ("arxiv", "pubmed", ...).
    Category / CategoryVocabulary, arxiv_category(), mesh_heading(): subject terms.
    IdentifierNamespace / PAPER_IDENTIFIERS: the identifier schemes a Paper can carry;
        add a new paper source by adding one entry.
    identifier_columns(): flat vertex columns for graph/upsert.py.

Usage: sources build `Paper(arxiv_id=..., ...)` or `Paper(pmid=..., ...)`; the flat kwargs
fold into `Paper.identifiers`, and `Paper.id` is the namespaced graph key.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Source(StrEnum):
    """An Enum for the ingestion path that a Paper came from."""

    ARXIV = "arxiv"
    KAGGLE = "kaggle"
    PUBMED = "pubmed"
    PUBMED_BASELINE = "pubmed_baseline"


@dataclass(frozen=True)
class IdentifierNamespace:
    """An identifier scheme that a Paper can be keyed by.
    
    Depending on the source, a paper can be identified by an arXiv ID, a PubMed ID,
    a Semantic Scholar ID, or something else.
    Instead of having each as an optional field, the model stores one dict:
        ``identifiers: dict[str, str]``.
    
    Attributes:
        column (str): The flat vertex column name where the ID is written to.
        prefix (str): The namespace prefix to each ID. eg. "arxiv:", "pmid:".
    """

    column: str
    prefix: str

    def graph_id(self, value: str) -> str:
        return f"{self.prefix}:{value}"


# Register identifier schemes.
# Order matters as it is the preference order for picking the canonical graph ID.
# A new paper source requires a new entry here.
PAPER_IDENTIFIERS: tuple[IdentifierNamespace, ...] = (
    IdentifierNamespace("arxiv_id", "arxiv"),
    IdentifierNamespace("pmid", "pmid"),
    IdentifierNamespace("s2_paper_id", "s2"), # Semantic Scholar (S2) is for citation data.
)

# Reverse lookup dicts used respectively for validation and the kwarg-folding trick below.
_NAMESPACES_BY_PREFIX = {ns.prefix: ns for ns in PAPER_IDENTIFIERS}
_NAMESPACES_BY_COLUMN = {ns.column: ns for ns in PAPER_IDENTIFIERS}


class CategoryVocabulary(StrEnum):
    """A controlled vocabulary that a Category's code belongs to."""

    ARXIV = "arxiv"
    MESH = "mesh"


class Category(BaseModel):
    """A paper's subject-classification term.

    One node type holds two unrelated vocabularies under a single unique index, so
    `code` is namespaced (e.g. "arxiv:cs.CL", "mesh:D009422") to keep them apart.

    Attributes:
        vocabulary (CategoryVocabulary): Which controlled vocabulary `code` belongs to.
        code (str): The namespaced code, unique across vocabularies.
        name (str): Display text. For MeSH this is not an identifier -- descriptors are
            keyed on their UI instead, since NLM renames them between releases.
    """

    vocabulary: CategoryVocabulary
    code: str
    name: str


def arxiv_category(code: str) -> Category:
    """Category for an arXiv taxonomy code (e.g. "cs.CL"), which is its own display name."""
    return Category(vocabulary=CategoryVocabulary.ARXIV, code=f"{CategoryVocabulary.ARXIV}:{code}", name=code)


def mesh_heading(ui: str, name: str) -> Category:
    """Category for a MeSH descriptor, keyed on its UI (e.g. "D009422") -- names are
    display-only since NLM renames descriptors between releases."""
    return Category(vocabulary=CategoryVocabulary.MESH, code=f"{CategoryVocabulary.MESH}:{ui}", name=name)


class Paper(BaseModel):
    """A research paper normalized from arXiv, PubMed, etc.

    Attributes:
        identifiers (dict[str, str]): Namespace prefix -> ID, e.g. {"arxiv": "2101.00001"}.
            Keys must be registered in PAPER_IDENTIFIERS. Also accepts the flat
            `arxiv_id=`/`pmid=`/`s2_paper_id=` constructor kwargs -- see
            `_fold_flat_identifiers`.
        primary_category (str | None): A namespaced Category.code.
    """

    identifiers: dict[str, str] = Field(default_factory=dict)

    title: str
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    primary_category: str | None = None

    published_date: date | None = None
    updated_date: date | None = None
    doi: str | None = None
    journal_ref: str | None = None
    comments: str | None = None

    source: Source = Source.ARXIV

    embedding: list[float] | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    influential_citation_count: int | None = None

    fetched_at: datetime | None = None
    enriched_at: datetime | None = None
    embedded_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _fold_flat_identifiers(cls, data):
        return _fold_flat_identifiers(data)

    @model_validator(mode="after")
    def _require_identifier(self) -> "Paper":
        _validate_identifiers(self.identifiers, "Paper")
        return self

    @property
    def id(self) -> str:
        """Returns the MERGE key: the highest-preference identifier present, namespaced
        per PAPER_IDENTIFIERS (e.g. "arxiv:2101.00001").
        """
        return _graph_id(self.identifiers, "Paper")

    @property
    def arxiv_id(self) -> str | None:
        return self.identifiers.get("arxiv")

    @property
    def pmid(self) -> str | None:
        return self.identifiers.get("pmid")

    @property
    def s2_paper_id(self) -> str | None:
        return self.identifiers.get("s2")

    def category_codes(self) -> list[str]:
        """Returns the namespaced codes alone.

        Returns:
            list[str]: What's stored on the Paper vertex's `categories` array, keeping
                `$category IN p.categories` queries working.
        """
        return [c.code for c in self.categories]


class CitationStub(BaseModel):
    """A minimal reference to a cited/citing paper.

    May or may not already exist as a full Paper node in the graph -- if not, it is
    upserted as a stub (is_stub=true) and filled in later if that paper is fully ingested.

    Attributes:
        identifiers (dict[str, str]): Namespace prefix -> ID, same scheme as `Paper`.
        title (str | None): The cited/citing paper's title, if known.
    """

    identifiers: dict[str, str] = Field(default_factory=dict)
    title: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _fold_flat_identifiers(cls, data):
        return _fold_flat_identifiers(data)

    @property
    def id(self) -> str:
        return _graph_id(self.identifiers, "CitationStub")

    @property
    def arxiv_id(self) -> str | None:
        return self.identifiers.get("arxiv")

    @property
    def pmid(self) -> str | None:
        return self.identifiers.get("pmid")

    @property
    def s2_paper_id(self) -> str | None:
        return self.identifiers.get("s2")


class EnrichmentResult(BaseModel):
    """The output of Semantic Scholar enrichment for one paper.

    Adds citation data that arXiv/PubMed don't inherently offer.

    Attributes:
        paper_id (str): The graph Paper.id this result applies to.
        references (list[CitationStub]): Papers this one cites.
        citations (list[CitationStub]): Papers that cite this one.
    """

    paper_id: str
    s2_paper_id: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    influential_citation_count: int | None = None
    references: list[CitationStub] = Field(default_factory=list)
    citations: list[CitationStub] = Field(default_factory=list)
    enriched_at: datetime | None = None


def _fold_flat_identifiers(data):
    """Folds flat `arxiv_id=`/`pmid=`/`s2_paper_id=` kwargs into `identifiers`, so callers
    can write `Paper(arxiv_id=...)` instead of `Paper(identifiers={"arxiv": ...})`."""
    if not isinstance(data, dict):
        return data
    flat = {column: data.pop(column) for column in list(data) if column in _NAMESPACES_BY_COLUMN}
    if not flat:
        return data
    identifiers = dict(data.get("identifiers") or {})
    for column, value in flat.items():
        if value:
            identifiers[_NAMESPACES_BY_COLUMN[column].prefix] = str(value)
    data["identifiers"] = identifiers
    return data


def _validate_identifiers(identifiers: dict[str, str], model_name: str) -> None:
    """Raises ValueError unless every prefix is registered and at least one ID is set."""
    unknown = set(identifiers) - set(_NAMESPACES_BY_PREFIX)
    if unknown:
        raise ValueError(f"{model_name} has unregistered identifier namespace(s): {sorted(unknown)}")
    if not identifiers:
        raise ValueError(f"{model_name} needs at least one of {[ns.prefix for ns in PAPER_IDENTIFIERS]}")


def _graph_id(identifiers: dict[str, str], model_name: str) -> str:
    """The canonical graph ID: the first identifier present in PAPER_IDENTIFIERS'
    preference order, namespaced (e.g. "arxiv:2101.00001")."""
    _validate_identifiers(identifiers, model_name)
    for namespace in PAPER_IDENTIFIERS:
        value = identifiers.get(namespace.prefix)
        if value:
            return namespace.graph_id(value)
    raise ValueError(f"{model_name} needs at least one of {[ns.prefix for ns in PAPER_IDENTIFIERS]}")


def identifier_columns(identifiers: dict[str, str]) -> Iterator[tuple[str, str | None]]:
    """Yields (vertex column, value) for every namespace, `None` where absent -- the paper
    upsert SETs all columns unconditionally, so omitting one would leave a stale value."""
    for namespace in PAPER_IDENTIFIERS:
        yield namespace.column, identifiers.get(namespace.prefix)
