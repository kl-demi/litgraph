from spokebio.ingest.go import ensure_obo_file, extract_pathways, iter_term_stanzas
from spokebio.models import Pathway
from spokebio.upsert import upsert_pathways

_OBO_FIXTURE = """format-version: 1.2
data-version: releases/2026-06-15

[Term]
id: GO:0009611
name: response to wounding
namespace: biological_process
def: "Any process that results in a change in state or activity of a cell or an organism (in terms of movement, secretion, enzyme production, gene expression, etc.) as a result of a wounding stimulus." [GOC:go_curators]

[Term]
id: GO:0003674
name: molecular_function
namespace: molecular_function

[Term]
id: GO:0000002
name: obsolete mitochondrial genome maintenance
namespace: biological_process
is_obsolete: true

[Typedef]
id: part_of
name: part of

[Term]
id: GO:0009414
name: response to water deprivation
namespace: biological_process
"""


def test_iter_term_stanzas_parses_fields(tmp_path):
    obo_file = tmp_path / "go-basic.obo"
    obo_file.write_text(_OBO_FIXTURE)

    stanzas = list(iter_term_stanzas(obo_file))

    assert len(stanzas) == 4  # 4 [Term] stanzas; the [Typedef] is skipped entirely
    assert stanzas[0] == {
        "id": "GO:0009611",
        "name": "response to wounding",
        "namespace": "biological_process",
        "is_obsolete": False,
    }
    assert stanzas[2]["is_obsolete"] is True


def test_extract_pathways_keeps_only_non_obsolete_biological_process(tmp_path):
    obo_file = tmp_path / "go-basic.obo"
    obo_file.write_text(_OBO_FIXTURE)

    pathways = list(extract_pathways(iter_term_stanzas(obo_file)))

    assert pathways == [
        Pathway(pathway_id="GO:0009611", name="response to wounding", source_db="GO"),
        Pathway(pathway_id="GO:0009414", name="response to water deprivation", source_db="GO"),
    ]


def test_ensure_obo_file_skips_download_if_already_cached(tmp_path, mocker):
    obo_file = tmp_path / "go-basic.obo"
    obo_file.write_text(_OBO_FIXTURE)
    mock_stream = mocker.patch("spokebio.ingest.go.httpx.stream")

    result = ensure_obo_file(obo_file)

    assert result == str(obo_file)
    mock_stream.assert_not_called()


def test_ensure_obo_file_downloads_when_missing(tmp_path, mocker):
    obo_file = tmp_path / "subdir" / "go-basic.obo"

    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield _OBO_FIXTURE.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mocker.patch("spokebio.ingest.go.httpx.stream", return_value=FakeStreamResponse())

    result = ensure_obo_file(obo_file)

    assert result == str(obo_file)
    assert obo_file.read_text() == _OBO_FIXTURE


def test_ensure_obo_file_force_redownloads(tmp_path, mocker):
    obo_file = tmp_path / "go-basic.obo"
    obo_file.write_text("stale content")

    class FakeStreamResponse:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"fresh content"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    mock_stream = mocker.patch("spokebio.ingest.go.httpx.stream", return_value=FakeStreamResponse())

    ensure_obo_file(obo_file, force=True)

    mock_stream.assert_called_once()
    assert obo_file.read_text() == "fresh content"


def test_upsert_pathways_writes_params(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    mock_run_write.return_value = [{"new_pathways": 2}]

    pathways = [
        Pathway(pathway_id="GO:0009611", name="response to wounding", source_db="GO"),
        Pathway(pathway_id="GO:0009414", name="response to water deprivation", source_db="GO"),
    ]

    new_count = upsert_pathways(pathways)

    assert new_count == 2
    call = mock_run_write.call_args
    assert call.kwargs["pathways"][0] == {"pathway_id": "GO:0009611", "name": "response to wounding", "source_db": "GO"}


def test_upsert_pathways_noop_on_empty(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    assert upsert_pathways([]) == 0
    mock_run_write.assert_not_called()
