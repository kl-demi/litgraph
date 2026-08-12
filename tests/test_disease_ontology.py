from spokebio.ingest.disease_ontology import (
    DOID_OBO_URL,
    ensure_doid_file,
    extract_disease_xrefs,
    extract_is_a_edges,
    iter_term_stanzas,
)
from spokebio.models import DiseaseIsA, DiseaseXref
from spokebio.upsert import upsert_disease_is_a, upsert_disease_xrefs

# DOID:1 -> DOID:2 -> DOID:3 puts an unmapped term between two mapped ones, so the
# hierarchy projection has to walk through it. DOID:4 is obsolete, DOID:5 unmapped-leaf.
_OBO_FIXTURE = """format-version: 1.2
data-version: releases/2026-07-31/doid.obo

[Term]
id: DOID:1
name: lung carcinoma
xref: MESH:D002289
xref: UMLS_CUI:C0007131
is_a: DOID:2 ! thoracic cancer

[Term]
id: DOID:2
name: thoracic cancer
is_a: DOID:3 ! neoplasm

[Term]
id: DOID:3
name: neoplasm
xref: MESH:D009369

[Typedef]
id: part_of
name: part of

[Term]
id: DOID:4
name: obsolete lung thing
xref: MESH:D002289
is_obsolete: true

[Term]
id: DOID:5
name: unmapped disease
is_a: DOID:3 ! neoplasm
"""


def _stanzas(tmp_path):
    obo_file = tmp_path / "doid.obo"
    obo_file.write_text(_OBO_FIXTURE)
    return lambda: iter_term_stanzas(obo_file)


def test_iter_term_stanzas_collects_multi_valued_fields(tmp_path):
    stanzas = list(_stanzas(tmp_path)())

    assert len(stanzas) == 5  # 5 [Term] stanzas; the [Typedef] is skipped entirely
    assert stanzas[0] == {
        "id": "DOID:1",
        "name": "lung carcinoma",
        "is_obsolete": False,
        "xref": ["MESH:D002289", "UMLS_CUI:C0007131"],
        "is_a": ["DOID:2"],  # the trailing "! thoracic cancer" comment is dropped
    }
    assert stanzas[3]["is_obsolete"] is True


def test_extract_disease_xrefs_keys_on_mesh_and_ignores_other_vocabularies(tmp_path):
    xrefs = sorted(extract_disease_xrefs(_stanzas(tmp_path)()), key=lambda x: x.disease_id)

    assert xrefs == [
        DiseaseXref(disease_id="mesh:D002289", doid="DOID:1", name="lung carcinoma"),
        DiseaseXref(disease_id="mesh:D009369", doid="DOID:3", name="neoplasm"),
    ]


def test_extract_disease_xrefs_skips_obsolete_terms(tmp_path):
    """DOID:4 carries the same MeSH id as DOID:1 but is obsolete, so it must not win."""
    by_id = {x.disease_id: x for x in extract_disease_xrefs(_stanzas(tmp_path)())}

    assert by_id["mesh:D002289"].doid == "DOID:1"


def test_extract_disease_xrefs_breaks_ties_deterministically():
    """A MeSH id several DOIDs claim resolves to the smallest, so a re-run is stable."""
    stanzas = [
        {"id": "DOID:70", "name": "later", "is_obsolete": False, "xref": ["MESH:D000001"], "is_a": []},
        {"id": "DOID:12", "name": "earlier", "is_obsolete": False, "xref": ["MESH:D000001"], "is_a": []},
    ]
    assert [x.doid for x in extract_disease_xrefs(stanzas)] == ["DOID:12"]


def test_extract_is_a_edges_walks_through_unmapped_intermediates(tmp_path):
    """DOID:1 -> DOID:2 (no MeSH) -> DOID:3. Projecting only edges whose two ends both
    map would lose this entirely; the nearest mapped ancestor is DOID:3."""
    edges = list(extract_is_a_edges(_stanzas(tmp_path)()))

    assert edges == [DiseaseIsA(child_id="mesh:D002289", parent_id="mesh:D009369")]


def test_extract_is_a_edges_skips_unmapped_children(tmp_path):
    """DOID:5 has a mapped parent but no MeSH id of its own, so there is no node to hang
    the edge on."""
    edges = list(extract_is_a_edges(_stanzas(tmp_path)()))

    assert all(e.child_id != "mesh:D009369" for e in edges)


def test_extract_is_a_edges_drops_self_edges():
    """Two DO terms sharing a MeSH id would otherwise project onto a self-edge."""
    stanzas = [
        {"id": "DOID:1", "name": "child", "is_obsolete": False, "xref": ["MESH:D1"], "is_a": ["DOID:2"]},
        {"id": "DOID:2", "name": "parent", "is_obsolete": False, "xref": ["MESH:D1"], "is_a": []},
    ]
    assert list(extract_is_a_edges(stanzas)) == []


# Download mechanics (skip-if-cached, retry, force) are covered once for every
# source in test_download.py; this only checks the URL/path wiring.
def test_ensure_doid_file_wires_the_do_url(tmp_path, mocker):
    ensure_cached = mocker.patch(
        "spokebio.ingest.disease_ontology.ensure_cached_file", return_value="path"
    )
    path = tmp_path / "doid.obo"

    result = ensure_doid_file(path, force=True)

    assert result == "path"
    ensure_cached.assert_called_once_with(DOID_OBO_URL, path, True)


def test_upsert_disease_xrefs_updates_on_match(mocker):
    """DO is the authority for a disease's identity, so its label supersedes PubTator's."""
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)

    new_count = upsert_disease_xrefs(
        [DiseaseXref(disease_id="mesh:D003920", doid="DOID:9351", name="diabetes mellitus")]
    )

    assert new_count == 1
    node_type, rows = mock_nodes.call_args.args
    assert node_type == "Disease"
    assert rows == [{"disease_id": "mesh:D003920", "doid": "DOID:9351", "name": "diabetes mellitus"}]
    assert mock_nodes.call_args.kwargs["update_existing"] is True


def test_upsert_disease_is_a_bootstraps_neither_endpoint(mocker):
    """The xref pass must have created both nodes; a key-only Disease would hide that."""
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)

    upsert_disease_is_a([DiseaseIsA(child_id="mesh:D002289", parent_id="mesh:D009369")])

    edge_type, rows = mock_edges.call_args.args
    assert edge_type == "IS_A"
    assert rows == [{"src": "mesh:D002289", "dst": "mesh:D009369"}]
    assert mock_edges.call_args.kwargs["create_missing"].name == "NONE"


def test_upserts_noop_on_empty(mocker):
    mocker.patch("spokebio.upsert.upsert_nodes", return_value=0)
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=0)

    assert upsert_disease_xrefs([]) == 0
    assert upsert_disease_is_a([]) == 0
    assert mock_edges.call_args.args[1] == []
