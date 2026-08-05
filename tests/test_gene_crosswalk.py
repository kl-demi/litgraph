import gzip

from spokebio.ingest.gene_crosswalk import (
    build_gene_identifier_crosswalk,
    build_gene_locus_map,
    build_gene_name_map,
    build_gene_symbol_map,
    build_locus_identifier_crosswalk,
    build_locus_tag_crosswalk,
    ensure_gene_info_file,
    is_locus_id,
    is_provisional_name,
    iter_gene_info_rows,
)

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


# Rice rows, unlike the Arabidopsis fixture above: the RAP-DB locus id is filed under
# Other_designations, and LocusTag holds an assembly-scoped tag instead. This is what
# build_locus_identifier_crosswalk exists to handle.
_RICE_HEADER = _HEADER
_RICE_RAP_IN_OTHER_DESIGNATIONS = "39947\t4352133\tBPH9\tOsJ_36543\t-\t-\t12\t-\tresistance protein\tprotein-coding\t-\t-\t-\tuncharacterized protein LOC4352133|Os12g0559400\t20260706\t-"
_RICE_MSU_ROW = "39947\t4336000\tSOMEGENE\tOsJ_15001\t-\t-\t4\t-\tsome protein\tprotein-coding\t-\t-\t-\thypothetical protein LOC_Os04g35210\t20260706\t-"
# A RAP id embedded in a longer designation rather than pipe-delimited on its own.
_RICE_EMBEDDED_RAP = "39947\t4324283\tB3GENE\tOsJ_00123\t-\t-\t1\t-\tB3 domain protein\tprotein-coding\t-\t-\t-\tB3 domain-containing protein Os01g0234100-like\t20260706\t-"

_NEWENTRY_ROW = "39947\t3974662\tNEWENTRY\t-\t-\t-\t-\t-\t-\tother\t-\t-\t-\t-\t20260706\t-"

_RICE_FIXTURE = "\n".join([_RICE_HEADER, _RICE_RAP_IN_OTHER_DESIGNATIONS, _RICE_MSU_ROW, _RICE_EMBEDDED_RAP]) + "\n"


def test_build_locus_identifier_crosswalk_indexes_other_designations(tmp_path):
    """The column that carries rice RAP ids. build_gene_identifier_crosswalk doesn't read
    it, which is why Oryzabase resolution sat at 20.2% instead of 81.5%."""
    gene_info_file = tmp_path / "rice.gene_info"
    gene_info_file.write_text(_RICE_FIXTURE)

    crosswalk = build_locus_identifier_crosswalk(gene_info_file)

    assert crosswalk["OS12G0559400"] == "ncbigene:4352133"


def test_build_locus_identifier_crosswalk_indexes_msu_with_and_without_loc_prefix(tmp_path):
    gene_info_file = tmp_path / "rice.gene_info"
    gene_info_file.write_text(_RICE_FIXTURE)

    crosswalk = build_locus_identifier_crosswalk(gene_info_file)

    assert crosswalk["LOC_OS04G35210"] == "ncbigene:4336000"
    assert crosswalk["OS04G35210"] == "ncbigene:4336000"


def test_build_locus_identifier_crosswalk_finds_rap_id_embedded_in_a_designation(tmp_path):
    """"B3 domain-containing protein Os01g0234100-like" doesn't split cleanly on the pipe
    delimiter, so the bare id has to be matched by pattern."""
    gene_info_file = tmp_path / "rice.gene_info"
    gene_info_file.write_text(_RICE_FIXTURE)

    crosswalk = build_locus_identifier_crosswalk(gene_info_file)

    assert crosswalk["OS01G0234100"] == "ncbigene:4324283"


def test_build_locus_identifier_crosswalk_keys_are_uppercased(tmp_path):
    gene_info_file = tmp_path / "rice.gene_info"
    gene_info_file.write_text(_RICE_FIXTURE)

    crosswalk = build_locus_identifier_crosswalk(gene_info_file)

    assert all(k == k.upper() for k in crosswalk)
    assert "Os12g0559400" not in crosswalk


def test_build_locus_identifier_crosswalk_skips_placeholder_symbols(tmp_path):
    gene_info_file = tmp_path / "rice.gene_info"
    gene_info_file.write_text("\n".join([_RICE_HEADER, _NEWENTRY_ROW]) + "\n")

    crosswalk = build_locus_identifier_crosswalk(gene_info_file)

    assert "NEWENTRY" not in crosswalk


def test_build_gene_identifier_crosswalk_unchanged_by_the_new_builder(tmp_path):
    """The GAF path still depends on the narrower builder; broadening its keys would
    change which Gene nodes existing PARTICIPATES_IN edges resolve onto."""
    gene_info_file = tmp_path / "rice.gene_info"
    gene_info_file.write_text(_RICE_FIXTURE)

    narrow = build_gene_identifier_crosswalk(gene_info_file)

    assert "OS12G0559400" not in narrow
    assert narrow["OsJ_36543"] == "ncbigene:4352133"


# Rice's actual shape, which differs from Arabidopsis above in the ways that matter here:
# no Symbol_from_nomenclature_authority at all, Symbol is usually NCBI's "LOC<GeneID>"
# placeholder, and the RAP-DB locus id lives in Other_designations.
_RICE_REAL_SYMBOL_ROW = "39947\t4324813\tPHT4;3\t-\t-\t-\t1\t-\tphosphate transporter\tprotein-coding\t-\t-\t-\tOs01g0107400\t20260706\t-"
_RICE_LOC_PLACEHOLDER_ROW = "39947\t4323840\tLOC4323840\tOsJ_01234\t-\t-\t1\t-\tuncharacterized\tprotein-coding\t-\t-\t-\tuncharacterized protein|Os01g0970700\t20260706\t-"
_RICE_NOTHING_USABLE_ROW = "39947\t4399999\tLOC4399999\t-\t-\t-\t1\t-\tuncharacterized\tprotein-coding\t-\t-\t-\thypothetical protein\t20260706\t-"
_RICE_NEWENTRY_ROW = "39947\t4326819\tNEWENTRY\t-\t-\t-\t1\t-\tRecord to support submission\tother\t-\t-\t-\t-\t20260706\t-"

_RICE_NAME_FIXTURE = (
    "\n".join(
        [_HEADER, _RICE_REAL_SYMBOL_ROW, _RICE_LOC_PLACEHOLDER_ROW, _RICE_NOTHING_USABLE_ROW, _RICE_NEWENTRY_ROW]
    )
    + "\n"
)


def _rice_gene_info(tmp_path):
    path = tmp_path / "Oryza_sativa.gene_info"
    path.write_text(_RICE_NAME_FIXTURE)
    return path


def test_gene_name_map_prefers_a_real_symbol(tmp_path):
    assert build_gene_name_map(_rice_gene_info(tmp_path))["ncbigene:4324813"] == "PHT4;3"


def test_gene_name_map_falls_back_to_the_rap_locus_id(tmp_path):
    """Not a symbol, but the identifier rice researchers search on -- and far more use as a
    display name than the bare key."""
    assert build_gene_name_map(_rice_gene_info(tmp_path))["ncbigene:4323840"] == "Os01g0970700"


def test_gene_name_map_never_emits_the_loc_placeholder(tmp_path):
    """NCBI's "LOC<GeneID>" Symbol only restates the key. Writing it would hide which genes
    genuinely lack a symbol and block a later real one, since the fill is null-only."""
    names = build_gene_name_map(_rice_gene_info(tmp_path))

    assert "ncbigene:4399999" not in names
    assert not any(v.startswith("LOC") and v[3:].isdigit() for v in names.values())


def test_gene_name_map_skips_newentry_placeholder(tmp_path):
    assert "ncbigene:4326819" not in build_gene_name_map(_rice_gene_info(tmp_path))


# MSU/TIGR ids are the only identifier for a large, largely disjoint set of rice genes
# (3,464 gene_info rows carry one, only 419 carry both an MSU and a RAP id), so dropping
# them strands those genes with no name at all.
_RICE_MSU_ONLY_ROW = "39947\t4323837\tLOC4323837\t-\t-\t-\t1\t-\tuncharacterized\tprotein-coding\t-\t-\t-\thypothetical protein LOC_Os01g73880\t20260706\t-"

_RICE_TIER_FIXTURE = (
    "\n".join([_HEADER, _RICE_REAL_SYMBOL_ROW, _RICE_LOC_PLACEHOLDER_ROW, _RICE_MSU_ONLY_ROW, _RICE_NOTHING_USABLE_ROW])
    + "\n"
)


def _tier_gene_info(tmp_path):
    path = tmp_path / "tiers.gene_info"
    path.write_text(_RICE_TIER_FIXTURE)
    return path


def test_gene_name_map_falls_back_to_msu_when_there_is_no_rap_id(tmp_path):
    assert build_gene_name_map(_tier_gene_info(tmp_path))["ncbigene:4323837"] == "LOC_Os01g73880"


def test_gene_name_map_prefers_rap_over_msu(tmp_path):
    """RAP-DB annotates the current IRGSP-1.0 reference; MSU/TIGR is the older system."""
    row = "39947\t4400001\tLOC4400001\t-\t-\t-\t1\t-\tx\tprotein-coding\t-\t-\t-\tprotein Os01g0111100|LOC_Os01g11111\t20260706\t-"
    path = tmp_path / "both.gene_info"
    path.write_text("\n".join([_HEADER, row]) + "\n")

    assert build_gene_name_map(path)["ncbigene:4400001"] == "Os01g0111100"


def test_gene_symbol_map_excludes_locus_ids_and_placeholders(tmp_path):
    """Kept separate from the locus map so a later pass can tell a curated name from a
    positional fallback -- which is what makes upgrading safe."""
    symbols = build_gene_symbol_map(_tier_gene_info(tmp_path))

    assert symbols == {"ncbigene:4324813": "PHT4;3"}


def test_gene_locus_map_covers_genes_with_no_symbol(tmp_path):
    loci = build_gene_locus_map(_tier_gene_info(tmp_path))

    assert loci["ncbigene:4323840"] == "Os01g0970700"
    assert loci["ncbigene:4323837"] == "LOC_Os01g73880"
    assert "ncbigene:4399999" not in loci  # no locus id of either kind


def test_is_locus_id_distinguishes_fallbacks_from_symbols():
    assert is_locus_id("Os01g0970700")
    assert is_locus_id("LOC_Os01g73880")
    # NCBI's placeholder is not a locus id, and neither are real symbols.
    assert not is_locus_id("LOC4338919")
    assert not is_locus_id("SD1")
    assert not is_locus_id("PHT4;3")


def test_is_provisional_name_also_covers_the_ncbi_placeholder(tmp_path):
    """Extractors relay NCBI's LOC<GeneID> verbatim, and it is the key restated rather than a
    name -- so a curated symbol may replace it, unlike a real symbol."""
    assert is_provisional_name("LOC4338919")
    assert is_provisional_name("Os01g0970700")
    assert is_provisional_name("LOC_Os01g73880")
    assert not is_provisional_name("SD1")
    assert not is_provisional_name("PHT4;3")
    # ...but it is still not a *locus id*, which is a narrower question.
    assert not is_locus_id("LOC4338919")
