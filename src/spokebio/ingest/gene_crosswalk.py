import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# NCBI publishes one flat file per organism under this directory -- free, no license/API
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
    """Build a LocusTag -> namespaced NCBI Gene ID map (e.g. "AT1G32640" ->
    "ncbigene:840158"), matching the existing Gene.gene_id namespacing (see
    ingest/pubtator.py) -- the lookup a future GAF/PGDB ingestion needs to resolve a
    TAIR-style gene reference back to the Gene node PubTator3 already wrote, instead of
    minting a duplicate keyed by locus tag. Rows with no LocusTag (some gene_info rows
    use "-" for missing fields) are skipped -- nothing to cross-walk for those.
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
    Synonyms -- which raises the share of a GAF's gene references that resolve (measured
    on rice: 68.3% -> 83.6%), since a GAF doesn't consistently use the locus tag.

    LocusTag wins on collision (it is the authoritative locus identifier; confirmed on
    rice that no LocusTag is another gene's symbol/synonym). Symbols/synonyms that are
    ambiguous across genes are dropped rather than resolved arbitrarily -- on rice that's
    1.03% of tokens, including chloroplast genes like ``psbA`` present in several
    assemblies, where guessing would attach the edge to the wrong Gene node.
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
