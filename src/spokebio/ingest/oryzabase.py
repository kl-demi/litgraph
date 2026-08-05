"""Oryzabase loader: Gene -> Trait ASSOCIATED_WITH edges for rice.

Oryzabase (NIG, Japan) is the curated rice gene database. Its gene-list export carries
Trait Ontology annotations per gene, which is what makes a trait-centric query possible
on a rice corpus -- there is no rice equivalent of the human sources SPOKE draws on for
disease linkage (DisGeNET, OMIM).

This is the trait-side counterpart to ingest/gaf.py: same shape (bulk export, resolve
each gene reference onto the existing ``ncbigene:`` Gene key, report drops per run), but
targeting Trait nodes from ingest/trait_ontology.py instead of Pathway nodes.
"""

import csv
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from spokebio.models import AssociatedWith

ORYZABASE_URL = "https://shigen.nig.ac.jp/rice/oryzabase/gene/download"
ORYZABASE_CLASSTAG = "GENE_EN_LIST"
DEFAULT_ORYZABASE_PATH = "data/oryzabase/gene_list.tsv"

# The response declares `charset=Windows-31J` but the bytes are UTF-8 (with a BOM).
# Honouring the declared charset raises UnicodeDecodeError partway through the file, so
# decode as utf-8-sig explicitly rather than trusting the Content-Type. `errors` is
# lenient because a handful of curator-entered cells carry stray bytes, and losing a
# character inside a free-text Explanation must not abort a 22K-row ingestion.
_ENCODING = "utf-8-sig"
_ENCODING_ERRORS = "replace"

_TO_ID = re.compile(r"TO:\d{7}")
# RAP-DB and MSU/TIGR locus ids. Extracted by regex rather than by splitting the cell:
# RAP values carry trailing whitespace, MSU values are comma-separated *and* suffixed
# with a transcript number ("LOC_Os12g37280.1, LOC_Os12g37290.1"), and the regex drops
# the suffix for free so the token matches gene_info's un-suffixed form.
_RAP_ID = re.compile(r"Os\d{2}g\d{7}")
_MSU_ID = re.compile(r"LOC_Os\d{2}g\d{5}")

_TRAIT_ONTOLOGY_COLUMN = "Trait Ontology"
_RAP_COLUMN = "RAP ID"
_MSU_COLUMN = "MSU ID"
_SYMBOL_COLUMN = "CGSNL Gene Symbol"
_SYNONYM_COLUMN = "Gene symbol synonym(s)"

# Oryzabase brackets classical mutant names it has no molecular identity for
# ("[CMS-54257]") and stars provisional symbols ("Bc6*"). Neither decoration appears in
# gene_info, so strip them before lookup.
_SYMBOL_DECORATIONS = "*[]() \t"


class OryzabaseExtraction(NamedTuple):
    """Extracted edges plus what got dropped. Reported per run for the same reason as
    gaf.py's GafExtraction: the drop rate depends on how well the species' gene_info
    file covers the identifiers this source happens to use."""

    edges: list[AssociatedWith]
    rows_with_traits: int
    dropped_unresolved: int
    dropped_duplicate: int
    dropped_unknown_trait: int


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
def ensure_oryzabase_file(path: str | Path = DEFAULT_ORYZABASE_PATH, force: bool = False) -> str:
    """Download Oryzabase's English gene list if not already cached. ~11MB, no license
    or API key needed for the download itself -- but confirm Oryzabase's citation terms
    before republishing anything derived from it."""
    p = Path(path)
    if p.exists() and not force:
        return str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream(
        "GET", ORYZABASE_URL, params={"classtag": ORYZABASE_CLASSTAG}, follow_redirects=True, timeout=180.0
    ) as response:
        response.raise_for_status()
        with p.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(p)


def iter_gene_rows(path: str | Path) -> Iterator[dict]:
    """Stream-parse the Oryzabase gene list: tab-delimited with a header row."""
    with open(path, encoding=_ENCODING, errors=_ENCODING_ERRORS, newline="") as f:
        yield from csv.DictReader(f, delimiter="\t")


def _gene_candidates(row: dict) -> Iterator[str]:
    """Identifiers a row offers for its gene, most authoritative first.

    Locus ids come before symbols deliberately: a symbol like ``CO`` or ``SALT`` is
    ambiguous against other genes and against ordinary words, whereas a RAP/MSU id
    identifies exactly one locus. Callers stop at the first hit, so ordering is what
    keeps an ambiguous symbol from claiming a row that has a clean locus id.
    """
    yield from _RAP_ID.findall(row.get(_RAP_COLUMN) or "")
    yield from _MSU_ID.findall(row.get(_MSU_COLUMN) or "")
    for column in (_SYMBOL_COLUMN, _SYNONYM_COLUMN):
        for token in re.split(r"[,;|]", row.get(column) or ""):
            token = token.strip(_SYMBOL_DECORATIONS)
            if token:
                yield token


def extract_associated_with(
    path: str | Path, crosswalk: dict[str, str], known_trait_ids: set[str] | None = None
) -> OryzabaseExtraction:
    """Parse the Oryzabase gene list into Gene -> Trait edges.

    ``crosswalk`` maps an uppercased identifier to an ``ncbigene:``-namespaced
    Gene.gene_id -- use ``gene_crosswalk.build_locus_identifier_crosswalk``, which
    indexes the Other_designations column where NCBI files rice RAP ids. Rows with no TO
    annotation are skipped; rows whose gene can't be resolved are counted and dropped
    rather than minting a Gene node under a locus-id key (docs/spoke_schema.md's Design
    Principle 5).

    ``known_trait_ids`` is the set of TO ids that exist as Trait nodes (i.e. what
    trait_ontology.extract_traits yields). Oryzabase annotates against TO ids that TO has
    since obsoleted -- 33 such terms, 637 edges (1.9%), against the 2026-01-14 release.
    Those edges would be dropped anyway, since upsert_associated_with MATCHes the Trait
    rather than creating it; passing this set makes the loss a reported number instead of
    a silent no-op inside a Cypher MATCH. Omit it to skip the check.

    TO ids are regex-extracted rather than split on the cell's commas, because a trait
    name can itself contain punctuation ("cytoplasmic male sterility (sensu Oryza)") and
    the inline names are redundant anyway -- ingest/trait_ontology.py is the authority
    for what a TO id is called.
    """
    edges: dict[tuple[str, str], AssociatedWith] = {}
    rows_with_traits = dropped_unresolved = dropped_duplicate = dropped_unknown_trait = 0

    for row in iter_gene_rows(path):
        trait_ids = dict.fromkeys(_TO_ID.findall(row.get(_TRAIT_ONTOLOGY_COLUMN) or ""))
        if not trait_ids:
            continue
        rows_with_traits += 1

        gene_id = next((crosswalk[c.upper()] for c in _gene_candidates(row) if c.upper() in crosswalk), None)
        if gene_id is None:
            dropped_unresolved += 1
            continue

        for trait_id in trait_ids:
            if known_trait_ids is not None and trait_id not in known_trait_ids:
                dropped_unknown_trait += 1
                continue
            key = (gene_id, trait_id)
            if key in edges:
                dropped_duplicate += 1
                continue
            edges[key] = AssociatedWith(gene_id=gene_id, trait_id=trait_id, source_db="Oryzabase")

    return OryzabaseExtraction(
        edges=list(edges.values()),
        rows_with_traits=rows_with_traits,
        dropped_unresolved=dropped_unresolved,
        dropped_duplicate=dropped_duplicate,
        dropped_unknown_trait=dropped_unknown_trait,
    )


# Oryzabase uses "_" for a row it has no CGSNL symbol for, alongside the usual dashes.
_MISSING_SYMBOLS = frozenset({"", "-", "_", "NONE"})


def build_symbol_map(path: str | Path, crosswalk: dict[str, str]) -> dict[str, str]:
    """Build an ``ncbigene:<id>`` -> curated CGSNL gene symbol map.

    This is the naming source rice actually has. Oryzabase's CGSNL is rice's nomenclature
    authority, but it does not feed NCBI, so gene_info's Symbol column is nearly all
    ``LOC<GeneID>`` placeholder while Oryzabase carries a real symbol on every one of its
    ~22K rows -- including the genes rice genetics is built on (``SD1``, ``XA21``, ``GHD7``,
    ``SUB1A``). Reads only the authoritative symbol column, not the synonyms, since this
    picks a single display name rather than building a lookup index.

    ``crosswalk`` maps an uppercased identifier to a Gene.gene_id -- use
    ``gene_crosswalk.build_locus_identifier_crosswalk``. Resolution goes through the same
    locus-ids-before-symbols ordering as ``_gene_candidates``, because a bare symbol like
    ``CO`` or ``SALT`` collides across genes while a RAP/MSU id names exactly one locus.
    """
    symbols: dict[str, str] = {}
    for row in iter_gene_rows(path):
        symbol = (row.get(_SYMBOL_COLUMN) or "").strip(_SYMBOL_DECORATIONS)
        if symbol.upper() in _MISSING_SYMBOLS:
            continue
        gene_id = next((crosswalk[c.upper()] for c in _gene_candidates(row) if c.upper() in crosswalk), None)
        if gene_id is not None:
            symbols.setdefault(gene_id, symbol)
    return symbols
