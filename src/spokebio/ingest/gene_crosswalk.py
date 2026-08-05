import gzip
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# NCBI publishes one file per organism under this directory -- free, no license/API
# key needed. Confirmed live (2026-07-24): file is named "<Genus>_<species>.gene_info.gz",
# e.g. "Arabidopsis_thaliana.gene_info.gz" (1.4MB, 38,313 rows).
GENE_INFO_BASE_URL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Plants"
DEFAULT_ORGANISM = "Arabidopsis_thaliana"
DEFAULT_GENE_INFO_DIR = "data/gene_info"


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
def ensure_gene_info_file(
    organism: str = DEFAULT_ORGANISM, dir_path: str | Path = DEFAULT_GENE_INFO_DIR, force: bool = False
) -> str:
    """Download NCBI's gene_info file for one organism -- its own NCBI GeneID <->
    LocusTag <-> Symbol/Synonym crosswalk -- if not already cached locally.
    """
    path = Path(dir_path) / f"{organism}.gene_info.gz"
    if path.exists() and not force:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{GENE_INFO_BASE_URL}/{organism}.gene_info.gz"
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with path.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return str(path)


def iter_gene_info_rows(path: str | Path) -> Iterator[dict]:
    """Stream-parse a gene_info(.gz) file: tab-delimited, first line is the header
    (prefixed with ``#``), one row per gene."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").lstrip("#").split("\t")
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != len(header):
                continue
            yield dict(zip(header, fields, strict=True))


def build_locus_tag_crosswalk(path: str | Path) -> dict[str, str]:
    """Build a LocusTag -> NCBI Gene ID map (e.g. "AT1G32640" -> "ncbigene:840158"), 
    matching the existing Gene.gene_id namespacing.
    
    This allows future GAF/PGDB ingestions to resolve a TAIR-style gene reference 
    back to the Gene node PubTator3 already wrote, instead of minting a duplicate 
    keyed by locus tag. Rows with no LocusTag are skipped.
    """
    crosswalk: dict[str, str] = {}
    for row in iter_gene_info_rows(path):
        locus_tag = row.get("LocusTag")
        gene_id = row.get("GeneID")
        if not locus_tag or locus_tag == "-" or not gene_id:
            continue
        crosswalk[locus_tag] = f"ncbigene:{gene_id}"
    return crosswalk


# NCBI uses this as a placeholder Symbol for un-named genes, so it is not an identifier
# for anything -- it collides across every gene that carries it.
_PLACEHOLDER_SYMBOLS = frozenset({"NEWENTRY"})


def build_gene_identifier_crosswalk(path: str | Path) -> dict[str, str]:
    """Like ``build_locus_tag_crosswalk``, but also indexes each gene's Symbol and
    Synonyms to resolve more GAF's gene references, since a GAF doesn't 
    consistently use the locus tag.

    LocusTag is the authoritative locus identifier (confirmed on rice that no 
    LocusTag is another gene's symbol/synonym). Symbols/synonyms that are
    ambiguous across genes are dropped -- on rice that's 1.03% of tokens.
    """
    locus_tags = build_locus_tag_crosswalk(path)

    candidates: dict[str, set[str]] = {}
    for row in iter_gene_info_rows(path):
        gene_id = row.get("GeneID")
        if not gene_id:
            continue
        for field in ("Symbol", "Synonyms"):
            value = row.get(field) or "-"
            if value == "-":
                continue
            for token in value.split("|"):
                token = token.strip()
                if token and token != "-" and token not in _PLACEHOLDER_SYMBOLS:
                    candidates.setdefault(token, set()).add(f"ncbigene:{gene_id}")

    crosswalk = {
        token: next(iter(gene_ids))
        for token, gene_ids in candidates.items()
        if len(gene_ids) == 1 and token not in locus_tags
    }
    crosswalk.update(locus_tags)
    return crosswalk


# Rice's community identifiers: RAP-DB ("Os01g0194300") and MSU/TIGR ("LOC_Os01g05060").
_RAP_ID = re.compile(r"Os\d{2}g\d{7}")
_MSU_ID = re.compile(r"LOC_Os\d{2}g\d{5}")

# Columns build_gene_identifier_crosswalk indexes, plus Other_designations. The addition
# matters more than it looks: NCBI files the RAP-DB locus id under Other_designations
# (22,408 of the 23,735 in rice's gene_info), *not* LocusTag -- rice LocusTags are
# assembly-scoped tags like "OsJ_01234"/"AKK66_gp001" instead. Indexing only
# LocusTag/Symbol/Synonyms resolves 20.2% of Oryzabase's trait-annotated genes; adding
# this column takes it to 81.5%.
_IDENTIFIER_COLUMNS = ("LocusTag", "Symbol", "Symbol_from_nomenclature_authority", "Synonyms", "Other_designations")


def build_locus_identifier_crosswalk(path: str | Path) -> dict[str, str]:
    """Build a case-insensitive identifier -> ``ncbigene:<id>`` map covering every
    identifier column in a gene_info file, including Other_designations, plus normalized
    RAP/MSU forms found anywhere in the row.

    Separate from ``build_gene_identifier_crosswalk`` rather than replacing it: that one
    backs the live GAF ingestion (``pipeline.run_gaf_ingest``), and broadening its keys
    would change which Gene nodes existing PARTICIPATES_IN edges resolve onto. Use this
    for sources that identify genes by community locus id -- see ingest/oryzabase.py.

    Keys are uppercased, so callers must uppercase their lookups. First writer wins per
    key, and LocusTag/Symbol are indexed first, so an authoritative identifier is never
    displaced by a synonym that happens to collide with it.
    """
    crosswalk: dict[str, str] = {}

    def add(token: str, gene_id: str) -> None:
        token = token.strip()
        if token and token != "-" and token not in _PLACEHOLDER_SYMBOLS:
            crosswalk.setdefault(token.upper(), f"ncbigene:{gene_id}")

    for row in iter_gene_info_rows(path):
        gene_id = row.get("GeneID")
        if not gene_id:
            continue
        for column in _IDENTIFIER_COLUMNS:
            value = row.get(column) or "-"
            if value == "-":
                continue
            # Other_designations and Synonyms are pipe-separated lists; the single-value
            # columns split to a one-element list harmlessly.
            for token in value.split("|"):
                add(token, gene_id)
        # A RAP/MSU id can be embedded in a longer designation ("uncharacterized protein
        # LOC4323834|Os01g0969000" splits cleanly, but "B3 domain-containing protein
        # Os01g0234100-like" does not), so also index the bare matches. MSU ids are
        # indexed with and without the LOC_ prefix, since sources use both spellings.
        joined = "\t".join(row.values())
        for match in _RAP_ID.findall(joined):
            add(match, gene_id)
        for match in _MSU_ID.findall(joined):
            add(match, gene_id)
            add(match.removeprefix("LOC_"), gene_id)

    return crosswalk


# NCBI's placeholder Symbol for a gene with no community symbol: "LOC" + the GeneID
# itself, e.g. "LOC4338919". Rice is almost entirely this -- 0 of its 39,963 gene_info
# rows carry a Symbol_from_nomenclature_authority, and only 41 of the genes currently in
# the graph have a real symbol. Writing it as a display name would be worse than writing
# nothing: it restates the key, hides which genes genuinely lack a symbol, and blocks a
# later real symbol from landing (the backfill only fills nulls).
_LOC_PLACEHOLDER_SYMBOL = re.compile(r"^LOC\d+$")
_BARE_RAP_ID = re.compile(r"^Os\d{2}g\d{7}$")
# MSU/TIGR's prefix is "LOC_", unrelated to NCBI's "LOC<GeneID>" placeholder above. Keep
# both patterns anchored: a `startswith("LOC")` test would throw away every MSU id.
_BARE_MSU_ID = re.compile(r"^LOC_Os\d{2}g\d{5}$")


def is_locus_id(name: str) -> bool:
    """Whether a display name is a bare locus id rather than a gene symbol."""
    return bool(_BARE_RAP_ID.match(name) or _BARE_MSU_ID.match(name))


def is_provisional_name(name: str) -> bool:
    """Whether a display name carries no more information than the key it hangs off.

    True for a bare locus id and for NCBI's ``LOC<GeneID>`` placeholder. Lets a later pass
    tell "nobody actually named this" from "a curator or extractor named this", which is what
    makes overwriting a name safe (see ``upsert.upgrade_gene_names``).

    The placeholder counts because it is not a name at all -- it is the GeneID restated, and
    extractors relay it verbatim (367 rice Gene nodes carry one from PubTator). Replacing it
    with a curated symbol loses nothing.
    """
    return is_locus_id(name) or bool(_LOC_PLACEHOLDER_SYMBOL.match(name))


def build_gene_symbol_map(path: str | Path) -> dict[str, str]:
    """Build an ``ncbigene:<id>`` -> real gene symbol map from a gene_info file.

    Real symbols only -- NCBI's ``LOC<GeneID>`` placeholder and its ``NEWENTRY`` filler are
    excluded, so a hit here is always a name someone actually assigned. Rice has very few
    (646 of 30,133 protein-coding rows, and over half of those are organellar genes carrying
    conventional plastid/mitochondrial symbols): rice's nomenclature authority, Oryzabase's
    CGSNL, does not feed NCBI, leaving Symbol_from_nomenclature_authority empty on all
    39,965 rows. See ingest/oryzabase.py for the source that does have them.
    """
    symbols: dict[str, str] = {}
    for row in iter_gene_info_rows(path):
        gene_id = row.get("GeneID")
        if not gene_id:
            continue
        symbol = (row.get("Symbol") or "-").strip()
        if symbol in ("-", "") or symbol in _PLACEHOLDER_SYMBOLS or _LOC_PLACEHOLDER_SYMBOL.match(symbol):
            continue
        symbols[f"ncbigene:{gene_id}"] = symbol
    return symbols


def build_gene_locus_map(path: str | Path) -> dict[str, str]:
    """Build an ``ncbigene:<id>`` -> locus id map from a gene_info file, RAP-DB preferred.

    Not symbols, but the identifiers rice researchers search on, and far more use than the
    bare key. RAP-DB (``Os01g0970700``) comes first because it annotates the current IRGSP-1.0
    reference; MSU/TIGR (``LOC_Os01g73770``) is the older system most tools have migrated
    away from, but it covers genes RAP-DB does not -- of rice's gene_info rows, 22,459 carry
    a RAP id and 3,464 an MSU id, with only 419 carrying both, so the two are largely
    disjoint and dropping MSU would strand its genes.
    """
    loci: dict[str, str] = {}
    for row in iter_gene_info_rows(path):
        gene_id = row.get("GeneID")
        if not gene_id:
            continue
        joined = "\t".join(row.values())
        match = _RAP_ID.search(joined) or _MSU_ID.search(joined)
        if match:
            loci[f"ncbigene:{gene_id}"] = match.group()
    return loci


def build_gene_name_map(path: str | Path) -> dict[str, str]:
    """Build an ``ncbigene:<id>`` -> display-name map from a gene_info file, for giving
    key-only Gene nodes something readable.

    A real ``Symbol`` when NCBI has one, else a locus id (RAP-DB, then MSU/TIGR). Genes whose
    only offer is NCBI's ``LOC<id>`` placeholder are **left out**, so they stay null and
    remain fillable later. Writing it would restate the key and hide which genes genuinely
    lack a symbol.

    This file is the weakest of the three naming sources for rice -- see
    ``build_gene_symbol_map`` for why -- so ``pipeline.run_gene_name_backfill`` layers
    Oryzabase's curated symbols above it rather than calling this alone.
    """
    return build_gene_locus_map(path) | build_gene_symbol_map(path)
