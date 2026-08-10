from spokebio.ingest.go import GO_OBO_URL, ensure_obo_file, extract_pathways, iter_term_stanzas
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


# Download mechanics (skip-if-cached, retry, force) are covered once for every
# source in test_download.py; this only checks the URL/path wiring.
def test_ensure_obo_file_wires_the_go_url(tmp_path, mocker):
    ensure_cached = mocker.patch("spokebio.ingest.go.ensure_cached_file", return_value="path")
    path = tmp_path / "go-basic.obo"

    result = ensure_obo_file(path, force=True)

    assert result == "path"
    ensure_cached.assert_called_once_with(GO_OBO_URL, path, True)


def test_upsert_pathways_writes_params(mocker):
    """GO is the authority for its own terms, so a matched Pathway is updated."""
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes", return_value=2)

    new_count = upsert_pathways(
        [
            Pathway(pathway_id="GO:0009611", name="response to wounding", source_db="GO"),
            Pathway(pathway_id="GO:0009414", name="response to water deprivation", source_db="GO"),
        ]
    )

    assert new_count == 2
    node_type, rows = mock_nodes.call_args.args
    assert node_type == "Pathway"
    assert rows[0] == {"pathway_id": "GO:0009611", "name": "response to wounding", "source_db": "GO"}
    assert mock_nodes.call_args.kwargs["update_existing"] is True


def test_upsert_pathways_noop_on_empty(mocker):
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes", return_value=0)
    assert upsert_pathways([]) == 0
    assert mock_nodes.call_args.args[1] == []
