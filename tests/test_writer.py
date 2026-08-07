import pytest

import litgraph.db.schema  # noqa: F401  -- registers the core types
import spokebio.schema_ext  # noqa: F401  -- registers the biology types
from litgraph.graph import writer
from litgraph.graph.writer import CreateMissing, upsert_edges, upsert_nodes


@pytest.fixture
def arcadedb(mocker):
    mocker.patch.object(writer, "get_settings", return_value=mocker.Mock(graph_backend="arcadedb"))
    return mocker.patch.object(writer.arcadedb_http, "run_script", return_value=[{"value": 1}])


@pytest.fixture
def neo4j(mocker):
    mocker.patch.object(writer, "get_settings", return_value=mocker.Mock(graph_backend="neo4j"))
    return mocker.patch.object(writer, "run_write", return_value=[{"created": 1}])


def _sql(mock):
    return mock.call_args.args[0]


# --- Dispatch and shared behaviour -------------------------------------------------------


def test_nodes_noop_on_empty(arcadedb):
    assert upsert_nodes("Pathway", [], update_existing=True) == 0
    arcadedb.assert_not_called()


def test_edges_noop_on_empty(arcadedb):
    assert upsert_edges("CITES", [], create_missing=CreateMissing.NONE, update_existing=False) == 0
    arcadedb.assert_not_called()


def test_nodes_return_the_created_count(arcadedb):
    assert upsert_nodes("Pathway", [{"pathway_id": "GO:1"}], update_existing=True) == 1


def test_rows_are_passed_as_a_parameter_not_interpolated(arcadedb):
    upsert_nodes("Pathway", [{"pathway_id": "GO:1", "name": "x'; DROP"}], update_existing=True)
    assert arcadedb.call_args.kwargs["rows"] == [{"pathway_id": "GO:1", "name": "x'; DROP"}]


def test_unregistered_type_raises(arcadedb):
    with pytest.raises(KeyError):
        upsert_nodes("Nonexistent", [{"id": "x"}], update_existing=True)


def test_only_registered_properties_present_in_rows_are_written(arcadedb):
    """An optional property the rows omit must not be written as null."""
    upsert_nodes("Pathway", [{"pathway_id": "GO:1", "name": "Apoptosis"}], update_existing=True)
    sql = _sql(arcadedb)
    assert "name = $r.name" in sql
    assert "source_db" not in sql


def test_unregistered_row_keys_are_ignored(arcadedb):
    upsert_nodes("Pathway", [{"pathway_id": "GO:1", "bogus": 1}], update_existing=True)
    assert "bogus" not in _sql(arcadedb)


# --- Node policy -------------------------------------------------------------------------


def test_nodes_key_comes_from_the_registry(arcadedb):
    upsert_nodes("PubtatorChecked", [{"paper_id": "p1"}], update_existing=True)
    assert "SELECT FROM PubtatorChecked WHERE paper_id = $r.paper_id" in _sql(arcadedb)


def test_update_existing_rewrites_properties_on_match(arcadedb):
    upsert_nodes("Pathway", [{"pathway_id": "GO:1", "name": "N"}], update_existing=True)
    assert "UPDATE Pathway SET name = $r.name" in _sql(arcadedb)


def test_update_existing_false_only_inserts(arcadedb):
    """Entity nodes must not overwrite a name another job set."""
    upsert_nodes("Gene", [{"gene_id": "ncbigene:1", "name": "N"}], update_existing=False)
    sql = _sql(arcadedb)
    assert "INSERT INTO Gene" in sql
    assert "UPDATE Gene" not in sql


# --- Edge endpoints ----------------------------------------------------------------------


def test_endpoint_types_come_from_the_registry(arcadedb):
    upsert_edges(
        "PARTICIPATES_IN",
        [{"src": "ncbigene:1", "dst": "GO:1"}],
        create_missing=CreateMissing.SRC,
        update_existing=True,
    )
    sql = _sql(arcadedb)
    assert "SELECT FROM Gene WHERE gene_id = $r.src" in sql
    assert "SELECT FROM Pathway WHERE pathway_id = $r.dst" in sql


def test_create_missing_src_bootstraps_only_the_source(arcadedb):
    upsert_edges(
        "PARTICIPATES_IN",
        [{"src": "ncbigene:1", "dst": "GO:1"}],
        create_missing=CreateMissing.SRC,
        update_existing=True,
    )
    sql = _sql(arcadedb)
    assert "INSERT INTO Gene SET gene_id = $r.src" in sql
    assert "INSERT INTO Pathway" not in sql


def test_create_missing_dst_bootstraps_only_the_destination(arcadedb):
    upsert_edges(
        "PRODUCES", [{"src": "R-HSA-1", "dst": "mesh:D1"}], create_missing=CreateMissing.DST, update_existing=True
    )
    sql = _sql(arcadedb)
    assert "INSERT INTO Compound SET compound_id = $r.dst" in sql
    assert "INSERT INTO Pathway" not in sql


def test_create_missing_none_bootstraps_neither(arcadedb):
    upsert_edges("CITES", [{"src": "arxiv:a", "dst": "arxiv:b"}], create_missing=CreateMissing.NONE, update_existing=False)
    assert "INSERT INTO Paper" not in _sql(arcadedb)


def test_bootstrapping_a_non_bootstrappable_endpoint_raises(arcadedb):
    """Paper/Pathway don't declare bootstrappable=True, so the policy can't be
    loosened at a call site."""
    with pytest.raises(ValueError, match="cannot bootstrap Paper"):
        upsert_edges(
            "CITES", [{"src": "arxiv:a", "dst": "arxiv:b"}], create_missing=CreateMissing.BOTH, update_existing=False
        )
    with pytest.raises(ValueError, match="cannot bootstrap Pathway"):
        upsert_edges(
            "PARTICIPATES_IN",
            [{"src": "ncbigene:1", "dst": "GO:1"}],
            create_missing=CreateMissing.DST,
            update_existing=True,
        )
    arcadedb.assert_not_called()


def test_dst_override_targets_another_node_type(arcadedb):
    """MENTIONS is registered Paper -> Gene but is also written to Compound and Organism."""
    upsert_edges(
        "MENTIONS",
        [{"src": "pmid:1", "dst": "mesh:D1"}],
        create_missing=CreateMissing.NONE,
        update_existing=False,
        dst="Compound",
    )
    assert "SELECT FROM Compound WHERE compound_id = $r.dst" in _sql(arcadedb)


def test_self_edge_endpoints_do_not_collide(arcadedb):
    """CITES is Paper -> Paper, so rows use fixed src/dst names rather than the key prop."""
    upsert_edges("CITES", [{"src": "arxiv:a", "dst": "arxiv:b"}], create_missing=CreateMissing.NONE, update_existing=False)
    sql = _sql(arcadedb)
    assert "WHERE id = $r.src" in sql
    assert "WHERE id = $r.dst" in sql


# --- Edge properties ---------------------------------------------------------------------


def test_edge_properties_are_set_on_create(arcadedb):
    upsert_edges(
        "PARTICIPATES_IN",
        [{"src": "ncbigene:1", "dst": "GO:1", "evidence_code": "TAS"}],
        create_missing=CreateMissing.SRC,
        update_existing=True,
    )
    assert "CREATE EDGE PARTICIPATES_IN FROM $srcRid TO $dstRid SET evidence_code = $r.evidence_code" in _sql(arcadedb)


def test_update_existing_refreshes_edge_properties(arcadedb):
    upsert_edges(
        "PARTICIPATES_IN",
        [{"src": "ncbigene:1", "dst": "GO:1", "evidence_code": "TAS"}],
        create_missing=CreateMissing.SRC,
        update_existing=True,
    )
    assert "UPDATE EDGE PARTICIPATES_IN SET evidence_code = $r.evidence_code" in _sql(arcadedb)


def test_update_existing_false_never_restamps_an_edge(arcadedb):
    """Whichever extractor found a mention first keeps the `source` attribution."""
    upsert_edges(
        "MENTIONS",
        [{"src": "pmid:1", "dst": "ncbigene:1", "source": "pubtator3"}],
        create_missing=CreateMissing.NONE,
        update_existing=False,
    )
    sql = _sql(arcadedb)
    assert "SET source = $r.source" in sql
    assert "UPDATE EDGE" not in sql


def test_edge_is_only_created_when_absent(arcadedb):
    upsert_edges("CITES", [{"src": "arxiv:a", "dst": "arxiv:b"}], create_missing=CreateMissing.NONE, update_existing=False)
    sql = _sql(arcadedb)
    assert "SELECT FROM CITES WHERE @out = $srcRid AND @in = $dstRid" in sql
    assert "IF ($existing.size() = 0)" in sql


# --- Neo4j dialect -------------------------------------------------------------------------


def test_neo4j_nodes_merge_on_the_key(neo4j):
    upsert_nodes("Pathway", [{"pathway_id": "GO:1", "name": "N"}], update_existing=True)
    assert "MERGE (n:Pathway {pathway_id: r.pathway_id})" in _sql(neo4j)


def test_neo4j_create_missing_picks_merge_or_match_per_endpoint(neo4j):
    upsert_edges(
        "PARTICIPATES_IN",
        [{"src": "ncbigene:1", "dst": "GO:1"}],
        create_missing=CreateMissing.SRC,
        update_existing=True,
    )
    sql = _sql(neo4j)
    assert "MERGE (s:Gene {gene_id: r.src})" in sql
    assert "MATCH (d:Pathway {pathway_id: r.dst})" in sql


def test_neo4j_returns_the_created_count(neo4j):
    assert upsert_nodes("Pathway", [{"pathway_id": "GO:1"}], update_existing=True) == 1
    assert "AS created" in _sql(neo4j)
