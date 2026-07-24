from spokebio.ingest.reactome import ensure_reactome_file, extract_human_pathways, extract_participates_in
from spokebio.models import ParticipatesIn, Pathway
from spokebio.upsert import upsert_participates_in, upsert_pathways

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
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    mock_run_write.return_value = [{"new_pathways": 1}]

    upsert_pathways([Pathway(pathway_id="R-HSA-164843", name="2-LTR circle formation", source_db="Reactome")])

    call = mock_run_write.call_args
    assert call.kwargs["pathways"][0]["source_db"] == "Reactome"


def test_upsert_participates_in_writes_params(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    mock_run_write.return_value = [{"new_edges": 1}]

    new_count = upsert_participates_in(
        [ParticipatesIn(gene_id="ncbigene:7157", pathway_id="R-HSA-111448", evidence_code="TAS")]
    )

    assert new_count == 1
    call = mock_run_write.call_args
    assert call.kwargs["edges"][0] == {
        "gene_id": "ncbigene:7157",
        "pathway_id": "R-HSA-111448",
        "evidence_code": "TAS",
    }


def test_upsert_participates_in_noop_on_empty(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert upsert_participates_in([]) == 0
    mock_run_write.assert_not_called()
