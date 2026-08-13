from spokebio.ingest.reactome import (
    REACTOME_BASE_URL,
    ensure_reactome_file,
    extract_human_pathways,
    extract_participates_in,
    extract_pathway_go_mappings,
    extract_produces,
)
from litgraph.graph.writer import CreateMissing
from spokebio.models import ParticipatesIn, Pathway, PathwayGoMapping, Produces
from spokebio.upsert import upsert_participates_in, upsert_pathway_go_mappings, upsert_pathways, upsert_produces

_PATHWAYS_FIXTURE = (
    "R-HSA-164843\t2-LTR circle formation\tHomo sapiens\n"
    "R-HSA-909733\tSome mouse pathway\tMus musculus\n"
    "R-HSA-74217\tPurine salvage\tHomo sapiens\n"
)

# TP53 (7157) x R-HSA-1257604 appears twice with conflicting evidence codes -- confirmed
# live in the real file. Also includes a mouse row (species filter) and a gene that only
# has a single, unambiguous row.
_EDGES_FIXTURE = (
    "7157\tR-HSA-1257604\thttps://reactome.org/x\tPIP3 activates AKT signaling\tTAS\tHomo sapiens\n"
    "7157\tR-HSA-1257604\thttps://reactome.org/x\tPIP3 activates AKT signaling\tIEA\tHomo sapiens\n"
    "7157\tR-HSA-111448\thttps://reactome.org/x\tActivation of NOXA\tTAS\tHomo sapiens\n"
    "999\tR-HSA-000000\thttps://reactome.org/x\tSome mouse-only pathway\tTAS\tMus musculus\n"
)


def test_extract_human_pathways_filters_by_species(tmp_path):
    path = tmp_path / "ReactomePathways.txt"
    path.write_text(_PATHWAYS_FIXTURE)

    pathways = list(extract_human_pathways(path))

    assert pathways == [
        Pathway(pathway_id="R-HSA-164843", name="2-LTR circle formation", source_db="Reactome"),
        Pathway(pathway_id="R-HSA-74217", name="Purine salvage", source_db="Reactome"),
    ]


def test_extract_participates_in_filters_by_species_and_namespaces_gene_id(tmp_path):
    path = tmp_path / "NCBI2Reactome.txt"
    path.write_text(_EDGES_FIXTURE)

    edges = extract_participates_in(path).edges

    edge_by_pathway = {e.pathway_id: e for e in edges}
    assert set(edge_by_pathway) == {"R-HSA-1257604", "R-HSA-111448"}  # mouse row dropped
    assert edge_by_pathway["R-HSA-111448"].gene_id == "ncbigene:7157"


def test_extract_participates_in_prefers_higher_trust_evidence_code(tmp_path):
    path = tmp_path / "NCBI2Reactome.txt"
    path.write_text(_EDGES_FIXTURE)

    edges = extract_participates_in(path).edges

    duplicated = next(e for e in edges if e.pathway_id == "R-HSA-1257604")
    assert duplicated.evidence_code == "TAS"  # not IEA, even though it appears second


def test_extract_participates_in_dedupes_to_one_edge_per_pair(tmp_path):
    path = tmp_path / "NCBI2Reactome.txt"
    path.write_text(_EDGES_FIXTURE)

    edges = extract_participates_in(path).edges

    assert len(edges) == 2  # not 3 -- the TAS/IEA duplicate collapses to one


def test_extract_participates_in_counts_considered_and_dropped_rows(tmp_path):
    """The mouse row is excluded before `rows_considered`, matching gaf.py's convention
    of only counting rows past the coarse species/aspect filter."""
    path = tmp_path / "NCBI2Reactome.txt"
    path.write_text(_EDGES_FIXTURE)

    extraction = extract_participates_in(path)

    assert extraction.rows_considered == 3  # 3 human rows; the mouse row isn't counted at all
    assert extraction.dropped_duplicate == 1  # the IEA row, collapsed into the TAS one


# CHEBI:16480 (nitric oxide) x R-HSA-1237112 appears twice with conflicting evidence
# codes, mirroring NCBI2Reactome.txt's real duplication issue. CHEBI:99999999 has no
# crosswalk entry -- should be silently dropped, not errored.
_CHEBI_EDGES_FIXTURE = (
    "16480\tR-HSA-1237112\thttps://reactome.org/x\tNitric oxide pathway\tTAS\tHomo sapiens\n"
    "16480\tR-HSA-1237112\thttps://reactome.org/x\tNitric oxide pathway\tIEA\tHomo sapiens\n"
    "16480\tR-HSA-9634600\thttps://reactome.org/x\tOther nitric oxide pathway\tTAS\tHomo sapiens\n"
    "99999999\tR-HSA-000000\thttps://reactome.org/x\tUnresolvable compound\tTAS\tHomo sapiens\n"
    "16480\tR-HSA-mouse\thttps://reactome.org/x\tMouse-only row\tTAS\tMus musculus\n"
)
_CHEBI_CROSSWALK = {"CHEBI:16480": "mesh:D009569"}


def test_extract_produces_resolves_via_crosswalk_and_filters_species(tmp_path):
    path = tmp_path / "ChEBI2Reactome.txt"
    path.write_text(_CHEBI_EDGES_FIXTURE)

    edges = extract_produces(path, _CHEBI_CROSSWALK).edges

    edge_by_pathway = {e.pathway_id: e for e in edges}
    # unresolvable compound and mouse-only row both dropped
    assert set(edge_by_pathway) == {"R-HSA-1237112", "R-HSA-9634600"}
    assert edge_by_pathway["R-HSA-9634600"].compound_id == "mesh:D009569"


def test_extract_produces_prefers_higher_trust_evidence_code(tmp_path):
    path = tmp_path / "ChEBI2Reactome.txt"
    path.write_text(_CHEBI_EDGES_FIXTURE)

    edges = extract_produces(path, _CHEBI_CROSSWALK).edges

    duplicated = next(e for e in edges if e.pathway_id == "R-HSA-1237112")
    assert duplicated.evidence_code == "TAS"


def test_extract_produces_dedupes_to_one_edge_per_pair(tmp_path):
    path = tmp_path / "ChEBI2Reactome.txt"
    path.write_text(_CHEBI_EDGES_FIXTURE)

    edges = extract_produces(path, _CHEBI_CROSSWALK).edges

    assert len(edges) == 2  # not 3 -- the TAS/IEA duplicate collapses to one


def test_extract_produces_counts_considered_and_dropped_rows(tmp_path):
    """The mouse row isn't counted at all (excluded before rows_considered); the
    unresolvable-ChEBI row is human and counted, but dropped as unresolved."""
    path = tmp_path / "ChEBI2Reactome.txt"
    path.write_text(_CHEBI_EDGES_FIXTURE)

    extraction = extract_produces(path, _CHEBI_CROSSWALK)

    assert extraction.rows_considered == 4  # 4 human rows; the mouse row isn't counted at all
    assert extraction.dropped_unresolved == 1  # CHEBI:99999999, no crosswalk entry
    assert extraction.dropped_duplicate == 1  # the IEA row, collapsed into the TAS one


def test_upsert_produces_writes_params(mocker):
    """Bootstraps the Compound, whose mesh: id the crosswalk already resolved, but requires
    the Pathway to exist."""
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)

    new_count = upsert_produces(
        [Produces(compound_id="mesh:D009569", pathway_id="R-HSA-1237112", evidence_code="TAS")]
    )

    assert new_count == 1
    edge_type, rows = mock_edges.call_args.args
    assert edge_type == "PRODUCES"
    assert rows[0] == {"src": "R-HSA-1237112", "dst": "mesh:D009569", "evidence_code": "TAS"}
    assert mock_edges.call_args.kwargs["create_missing"] is CreateMissing.DST


def test_upsert_produces_noop_on_empty(mocker):
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=0)
    assert upsert_produces([]) == 0
    assert mock_edges.call_args.args[1] == []


# Unlike Reactome's other flat files, this one has a header row; a GO id can be the
# target of more than one Reactome pathway (GO:0006892 here), which is fan-in, not a
# duplicate pair to collapse.
_GO_MAPPING_FIXTURE = (
    "Identifier\tName\tGO_Term\n"
    "R-HSA-73843\t5-Phosphoribose 1-diphosphate biosynthesis\tGO:0006015\n"
    "R-HSA-382556\tABC-family protein mediated transport\tGO:0055085\n"
    "R-HSA-199991\tMembrane Trafficking\tGO:0006892\n"
    "R-HSA-199992\ttrans-Golgi Network Vesicle Budding\tGO:0006892\n"
)


def test_extract_pathway_go_mappings_skips_the_header_row(tmp_path):
    path = tmp_path / "Pathways2GoTerms_human.txt"
    path.write_text(_GO_MAPPING_FIXTURE)

    mappings = extract_pathway_go_mappings(path)

    assert PathwayGoMapping(reactome_pathway_id="Identifier", go_pathway_id="GO_Term") not in mappings
    assert len(mappings) == 4


def test_extract_pathway_go_mappings_allows_one_go_id_to_have_several_pathways(tmp_path):
    path = tmp_path / "Pathways2GoTerms_human.txt"
    path.write_text(_GO_MAPPING_FIXTURE)

    mappings = extract_pathway_go_mappings(path)

    fan_in = [m for m in mappings if m.go_pathway_id == "GO:0006892"]
    assert {m.reactome_pathway_id for m in fan_in} == {"R-HSA-199991", "R-HSA-199992"}


def test_upsert_pathway_go_mappings_writes_params(mocker):
    """Neither endpoint bootstraps -- both Pathway nodes must already exist."""
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)

    new_count = upsert_pathway_go_mappings(
        [PathwayGoMapping(reactome_pathway_id="R-HSA-73843", go_pathway_id="GO:0006015")]
    )

    assert new_count == 1
    edge_type, rows = mock_edges.call_args.args
    assert edge_type == "MAPS_TO"
    assert rows[0] == {"src": "R-HSA-73843", "dst": "GO:0006015"}
    assert mock_edges.call_args.kwargs["create_missing"] is CreateMissing.NONE


def test_upsert_pathway_go_mappings_noop_on_empty(mocker):
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=0)
    assert upsert_pathway_go_mappings([]) == 0
    assert mock_edges.call_args.args[1] == []


# Download mechanics are covered once for every source in test_download.py; this
# only checks the URL/path wiring.
def test_ensure_reactome_file_wires_the_filename_into_url_and_path(tmp_path, mocker):
    ensure_cached = mocker.patch("spokebio.ingest.reactome.ensure_cached_file", return_value="path")

    result = ensure_reactome_file("ReactomePathways.txt", dir_path=tmp_path, force=True)

    assert result == "path"
    ensure_cached.assert_called_once_with(
        f"{REACTOME_BASE_URL}/ReactomePathways.txt", tmp_path / "ReactomePathways.txt", True
    )


def test_upsert_pathways_still_works_for_reactome_source(mocker):
    """GO and Reactome share one Pathway type and key, told apart by source_db."""
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)

    upsert_pathways([Pathway(pathway_id="R-HSA-164843", name="2-LTR circle formation", source_db="Reactome")])

    assert mock_nodes.call_args.args[1][0]["source_db"] == "Reactome"


def test_upsert_participates_in_writes_params(mocker):
    """Bootstraps the Gene, since most annotated genes have no node until literature names
    one, but requires the Pathway to exist."""
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)

    new_count = upsert_participates_in(
        [ParticipatesIn(gene_id="ncbigene:7157", pathway_id="R-HSA-111448", evidence_code="TAS")]
    )

    assert new_count == 1
    edge_type, rows = mock_edges.call_args.args
    assert edge_type == "PARTICIPATES_IN"
    assert rows[0] == {"src": "ncbigene:7157", "dst": "R-HSA-111448", "evidence_code": "TAS"}
    assert mock_edges.call_args.kwargs["create_missing"] is CreateMissing.SRC


def test_upsert_participates_in_noop_on_empty(mocker):
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=0)
    assert upsert_participates_in([]) == 0
    assert mock_edges.call_args.args[1] == []
