import gzip

from spokebio.ingest.gaf import extract_participates_in, iter_gaf_rows
from spokebio.ingest.gene_crosswalk import build_gene_identifier_crosswalk


def _gaf_row(
    symbol: str,
    go_id: str,
    evidence: str,
    aspect: str = "P",
    qualifier: str = "involved_in",
    synonyms: str = "",
) -> str:
    """One GAF 2.2 row (17 tab-separated columns), only the fields this loader reads."""
    cols = [""] * 17
    cols[0], cols[1] = "UniProtKB", "A0A0N7KC65"
    cols[2], cols[3], cols[4] = symbol, qualifier, go_id
    cols[5], cols[6] = "GO_REF:0000033", evidence
    cols[8], cols[10] = aspect, synonyms
    cols[11], cols[12] = "protein", "taxon:39947"
    return "\t".join(cols) + "\n"


# Mirrors the real ORYSJ file: symbol is the RAP-DB locus id, and the same
# (gene, pathway) pair legitimately appears under two evidence codes.
_GAF_FIXTURE = (
    "!gaf-version: 2.2\n"
    "!generated-by: GOC\n"
    + _gaf_row("Os01g0104100", "GO:0006511", "IEA")
    + _gaf_row("Os01g0104100", "GO:0006511", "IMP")
    + _gaf_row("Os01g0104100", "GO:0009611", "IBA")
    + _gaf_row("Os01g0105900", "GO:0016301", "IDA", aspect="F")
    + _gaf_row("Os01g0105900", "GO:0005634", "IDA", aspect="C")
    + _gaf_row("Os01g0105900", "GO:0009611", "IDA", qualifier="NOT|involved_in")
    + _gaf_row("UNKNOWN_LOCUS", "GO:0009611", "IDA")
    + _gaf_row("no-symbol-here", "GO:0006511", "IDA", synonyms="Os01g0105900|OSNPB_010105900")
)

_CROSSWALK = {"Os01g0104100": "ncbigene:4326812", "Os01g0105900": "ncbigene:4326817"}

_GENE_INFO_FIXTURE = (
    "#tax_id\tGeneID\tSymbol\tLocusTag\tSynonyms\n"
    "39947\t4326812\tOs01g0104100\tOs01g0104100\tRING1\n"
    "39947\t4326817\tpsbA\t-\tOs01g0105900\n"
    "39947\t4326818\tpsbA\t-\tSOMETHING\n"
    "39947\t4326819\tNEWENTRY\t-\t-\n"
    "39947\t4326820\tWRKY45\tOs05g0322900\t-\n"
)


def test_iter_gaf_rows_skips_comments_and_keeps_17_column_rows(tmp_path):
    path = tmp_path / "ORYSJ-uniprot.gaf"
    path.write_text(_GAF_FIXTURE + "truncated\trow\n")

    rows = list(iter_gaf_rows(path))

    assert len(rows) == 8
    assert all(len(r) == 17 for r in rows)


def test_iter_gaf_rows_reads_gzip(tmp_path):
    path = tmp_path / "ORYSJ-uniprot.gaf.gz"
    with gzip.open(path, "wt") as f:
        f.write(_GAF_FIXTURE)

    assert len(list(iter_gaf_rows(path))) == 8


def test_extract_keeps_only_biological_process(tmp_path):
    """molecular_function and cellular_component rows have no Pathway node to point at --
    ingest/go.py loads the biological_process branch alone."""
    path = tmp_path / "x.gaf"
    path.write_text(_GAF_FIXTURE)

    result = extract_participates_in(path, _CROSSWALK)

    assert all(e.pathway_id in {"GO:0006511", "GO:0009611"} for e in result.edges)
    assert "GO:0016301" not in {e.pathway_id for e in result.edges}  # aspect F
    assert "GO:0005634" not in {e.pathway_id for e in result.edges}  # aspect C
    assert result.rows_considered == 6


def test_extract_drops_not_qualified_rows(tmp_path):
    """A NOT qualifier is a negative annotation -- keeping it would assert the opposite
    of what the source says."""
    path = tmp_path / "x.gaf"
    path.write_text(_GAF_FIXTURE)

    result = extract_participates_in(path, _CROSSWALK)

    assert result.dropped_negated == 1
    assert ("ncbigene:4326817", "GO:0009611") not in {(e.gene_id, e.pathway_id) for e in result.edges}


def test_extract_drops_genes_absent_from_crosswalk(tmp_path):
    path = tmp_path / "x.gaf"
    path.write_text(_GAF_FIXTURE)

    result = extract_participates_in(path, _CROSSWALK)

    assert result.dropped_unresolved == 1
    assert all(e.gene_id.startswith("ncbigene:") for e in result.edges)


def test_extract_falls_back_to_synonyms_column(tmp_path):
    """A GAF doesn't always put a resolvable id in the symbol column."""
    path = tmp_path / "x.gaf"
    path.write_text(_GAF_FIXTURE)

    result = extract_participates_in(path, _CROSSWALK)

    assert ("ncbigene:4326817", "GO:0006511") in {(e.gene_id, e.pathway_id) for e in result.edges}


def test_extract_prefers_higher_trust_evidence_code(tmp_path):
    """IMP (experimental) beats IEA (uncurated) for the same gene/pathway pair,
    regardless of file order."""
    path = tmp_path / "x.gaf"
    path.write_text(_GAF_FIXTURE)

    result = extract_participates_in(path, _CROSSWALK)

    edge = next(e for e in result.edges if (e.gene_id, e.pathway_id) == ("ncbigene:4326812", "GO:0006511"))
    assert edge.evidence_code == "IMP"
    assert result.dropped_duplicate == 1


def test_extract_dedupes_gene_pathway_pairs(tmp_path):
    path = tmp_path / "x.gaf"
    path.write_text(_GAF_FIXTURE)

    result = extract_participates_in(path, _CROSSWALK)

    keys = [(e.gene_id, e.pathway_id) for e in result.edges]
    assert len(keys) == len(set(keys))


def test_identifier_crosswalk_indexes_locus_tags_symbols_and_synonyms(tmp_path):
    path = tmp_path / "Oryza_sativa.gene_info"
    path.write_text(_GENE_INFO_FIXTURE)

    crosswalk = build_gene_identifier_crosswalk(path)

    assert crosswalk["Os01g0104100"] == "ncbigene:4326812"  # LocusTag
    assert crosswalk["Os05g0322900"] == "ncbigene:4326820"  # LocusTag
    assert crosswalk["RING1"] == "ncbigene:4326812"  # Synonym
    assert crosswalk["WRKY45"] == "ncbigene:4326820"  # Symbol
    assert crosswalk["Os01g0105900"] == "ncbigene:4326817"  # Synonym, no LocusTag on that row


def test_identifier_crosswalk_drops_ambiguous_and_placeholder_symbols(tmp_path):
    """psbA maps to two genes here (as it does across real chloroplast assemblies) and
    NEWENTRY is NCBI's placeholder for un-named genes -- resolving either would attach
    edges to an arbitrary Gene node."""
    path = tmp_path / "Oryza_sativa.gene_info"
    path.write_text(_GENE_INFO_FIXTURE)

    crosswalk = build_gene_identifier_crosswalk(path)

    assert "psbA" not in crosswalk
    assert "NEWENTRY" not in crosswalk
