import gzip

from spokebio.ingest.gene_crosswalk import build_locus_tag_crosswalk, ensure_gene_info_file, iter_gene_info_rows

_HEADER = "#tax_id\tGeneID\tSymbol\tLocusTag\tSynonyms\tdbXrefs\tchromosome\tmap_location\tdescription\ttype_of_gene\tSymbol_from_nomenclature_authority\tFull_name_from_nomenclature_authority\tNomenclature_status\tOther_designations\tModification_date\tFeature_type"
_MYC2_ROW = "3702\t840158\tMYC2\tAT1G32640\tATMYC2|F6N18.4|JAI1|JIN1\tAraport:AT1G32640|TAIR:AT1G32640\t1\t-\tBasic helix-loop-helix (bHLH) DNA-binding family protein\tprotein-coding\tMYC2\tBasic helix-loop-helix (bHLH) DNA-binding family protein\tO\t-\t20260706\t-"
_PDF12_ROW = "3702\t834469\tPDF1.2\tAT5G44420\tPDF1.2a\tAraport:AT5G44420|TAIR:AT5G44420\t5\t-\tdefensin-like protein\tprotein-coding\tPDF1.2\tdefensin-like protein\tO\t-\t20260706\t-"
# A row with no LocusTag (dash) -- some gene_info rows have this, e.g. for genes without
# an assigned locus tag; must be skipped rather than cross-walked to a bogus key.
_NO_LOCUS_TAG_ROW = "3702\t999999\tSOMEGENE\t-\t-\t-\t1\t-\tsome uncharacterized gene\tprotein-coding\t-\t-\t-\t-\t20260706\t-"

_FIXTURE = "\n".join([_HEADER, _MYC2_ROW, _PDF12_ROW, _NO_LOCUS_TAG_ROW]) + "\n"


def test_iter_gene_info_rows_parses_plain_text(tmp_path):
    gene_info_file = tmp_path / "test.gene_info"
    gene_info_file.write_text(_FIXTURE)

    rows = list(iter_gene_info_rows(gene_info_file))

    assert len(rows) == 3
    assert rows[0]["GeneID"] == "840158"
    assert rows[0]["LocusTag"] == "AT1G32640"
    assert rows[0]["Symbol"] == "MYC2"


def test_iter_gene_info_rows_parses_gzip(tmp_path):
    gene_info_file = tmp_path / "test.gene_info.gz"
    with gzip.open(gene_info_file, "wt", encoding="utf-8") as f:
        f.write(_FIXTURE)

    rows = list(iter_gene_info_rows(gene_info_file))

    assert len(rows) == 3
    assert rows[0]["GeneID"] == "840158"


def test_build_locus_tag_crosswalk_maps_locus_tag_to_namespaced_gene_id(tmp_path):
    gene_info_file = tmp_path / "test.gene_info"
    gene_info_file.write_text(_FIXTURE)

    crosswalk = build_locus_tag_crosswalk(gene_info_file)

    assert crosswalk == {
        "AT1G32640": "ncbigene:840158",
        "AT5G44420": "ncbigene:834469",
    }


def test_build_locus_tag_crosswalk_skips_rows_without_a_locus_tag(tmp_path):
    gene_info_file = tmp_path / "test.gene_info"
    gene_info_file.write_text(_FIXTURE)

    crosswalk = build_locus_tag_crosswalk(gene_info_file)

    assert "-" not in crosswalk
    assert len(crosswalk) == 2


def test_ensure_gene_info_file_skips_download_if_already_cached(tmp_path, mocker):
    organism_dir = tmp_path
    gene_info_file = organism_dir / "Arabidopsis_thaliana.gene_info.gz"
    gene_info_file.write_text(_FIXTURE)
    mock_stream = mocker.patch("spokebio.ingest.gene_crosswalk.httpx.stream")

    result = ensure_gene_info_file(organism="Arabidopsis_thaliana", dir_path=organism_dir)

    assert result == str(gene_info_file)
    mock_stream.assert_not_called()


def test_ensure_gene_info_file_downloads_when_missing(tmp_path, mocker):
    organism_dir = tmp_path / "subdir"

    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield _FIXTURE.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mocker.patch("spokebio.ingest.gene_crosswalk.httpx.stream", return_value=FakeStreamResponse())

    result = ensure_gene_info_file(organism="Arabidopsis_thaliana", dir_path=organism_dir)

    assert result == str(organism_dir / "Arabidopsis_thaliana.gene_info.gz")
    assert (organism_dir / "Arabidopsis_thaliana.gene_info.gz").read_text() == _FIXTURE
