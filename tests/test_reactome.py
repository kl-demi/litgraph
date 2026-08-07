from spokebio.ingest.reactome import (
    ensure_reactome_file,
    extract_human_pathways,
    extract_participates_in,
    extract_produces,
)
from litgraph.graph.writer import CreateMissing
from spokebio.models import ParticipatesIn, Pathway, Produces
from spokebio.upsert import upsert_participates_in, upsert_pathways, upsert_produces

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

    edges = extract_participates_in(path)

    edge_by_pathway = {e.pathway_id: e for e in edges}
    assert set(edge_by_pathway) == {"R-HSA-1257604", "R-HSA-111448"}  # mouse row dropped
    assert edge_by_pathway["R-HSA-111448"].gene_id == "ncbigene:7157"


def test_extract_participates_in_prefers_higher_trust_evidence_code(tmp_path):
    path = tmp_path / "NCBI2Reactome.txt"
    path.write_text(_EDGES_FIXTURE)

    edges = extract_participates_in(path)

    duplicated = next(e for e in edges if e.pathway_id == "R-HSA-1257604")
    assert duplicated.evidence_code == "TAS"  # not IEA, even though it appears second


def test_extract_participates_in_dedupes_to_one_edge_per_pair(tmp_path):
    path = tmp_path / "NCBI2Reactome.txt"
    path.write_text(_EDGES_FIXTURE)

    edges = extract_participates_in(path)

    assert len(edges) == 2  # not 3 -- the TAS/IEA duplicate collapses to one


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

    edges = extract_produces(path, _CHEBI_CROSSWALK)

    edge_by_pathway = {e.pathway_id: e for e in edges}
    # unresolvable compound and mouse-only row both dropped
    assert set(edge_by_pathway) == {"R-HSA-1237112", "R-HSA-9634600"}
    assert edge_by_pathway["R-HSA-9634600"].compound_id == "mesh:D009569"


def test_extract_produces_prefers_higher_trust_evidence_code(tmp_path):
    path = tmp_path / "ChEBI2Reactome.txt"
    path.write_text(_CHEBI_EDGES_FIXTURE)

    edges = extract_produces(path, _CHEBI_CROSSWALK)

    duplicated = next(e for e in edges if e.pathway_id == "R-HSA-1237112")
    assert duplicated.evidence_code == "TAS"


def test_extract_produces_dedupes_to_one_edge_per_pair(tmp_path):
    path = tmp_path / "ChEBI2Reactome.txt"
    path.write_text(_CHEBI_EDGES_FIXTURE)

    edges = extract_produces(path, _CHEBI_CROSSWALK)

    assert len(edges) == 2  # not 3 -- the TAS/IEA duplicate collapses to one


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


def test_ensure_reactome_file_skips_download_if_already_cached(tmp_path, mocker):
    path = tmp_path / "ReactomePathways.txt"
    path.write_text(_PATHWAYS_FIXTURE)
    mock_stream = mocker.patch("spokebio.ingest.reactome.httpx.stream")

    result = ensure_reactome_file("ReactomePathways.txt", dir_path=tmp_path)

    assert result == str(path)
    mock_stream.assert_not_called()


def test_ensure_reactome_file_downloads_when_missing(tmp_path, mocker):
    dir_path = tmp_path / "reactome"

    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield _PATHWAYS_FIXTURE.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mocker.patch("spokebio.ingest.reactome.httpx.stream", return_value=FakeStreamResponse())

    result = ensure_reactome_file("ReactomePathways.txt", dir_path=dir_path)

    assert result == str(dir_path / "ReactomePathways.txt")
    assert (dir_path / "ReactomePathways.txt").read_text() == _PATHWAYS_FIXTURE


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
