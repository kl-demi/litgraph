import json

from spokebio.release_check import check_and_reingest, default_state_path, load_state, save_state


def test_default_state_path_is_scoped_per_database(mocker):
    """A shared path would report a source as unchanged for a database that was never
    actually ingested, because the version was recorded while checking a different
    one -- see check_and_reingest's docstring."""
    settings = mocker.Mock(arcadedb_database="rice")
    mocker.patch("spokebio.release_check.get_settings", return_value=settings)

    rice_path = default_state_path()
    settings.arcadedb_database = "human"
    assert default_state_path() != rice_path
    assert "rice" in str(rice_path)


def test_load_state_returns_empty_dict_when_no_file(tmp_path):
    assert load_state(tmp_path / "missing.json") == {}


def test_save_state_then_load_state_round_trips(tmp_path):
    path = tmp_path / "nested" / "state.json"
    save_state(path, {"go": "releases/2026-07-26"})

    assert load_state(path) == {"go": "releases/2026-07-26"}


def _patch_releases(mocker, **releases):
    mocker.patch("spokebio.release_check.get_go_release", return_value=releases.get("go", "go-v1"))
    mocker.patch("spokebio.release_check.get_reactome_release", return_value=releases.get("reactome", "1"))
    mocker.patch(
        "spokebio.release_check.get_disease_ontology_release", return_value=releases.get("disease_ontology", "do-v1")
    )


def test_check_and_reingest_skips_unchanged_sources(mocker, tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"go": "go-v1", "reactome": "1", "disease_ontology": "do-v1"})
    _patch_releases(mocker)
    mock_go = mocker.patch("spokebio.release_check.run_go_ingest")
    mocker.patch("spokebio.release_check.run_reactome_ingest")
    mocker.patch("spokebio.release_check.run_disease_ontology_ingest")

    results = check_and_reingest(path)

    assert all(not r["changed"] for r in results.values())
    assert all(r["totals"] is None for r in results.values())
    mock_go.assert_not_called()


def test_check_and_reingest_reingests_only_the_changed_source(mocker, tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"go": "go-v0", "reactome": "1", "disease_ontology": "do-v1"})
    _patch_releases(mocker, go="go-v1")
    mock_go = mocker.patch("spokebio.release_check.run_go_ingest", return_value={"pathways_processed": 1})
    mock_reactome = mocker.patch("spokebio.release_check.run_reactome_ingest")
    mock_do = mocker.patch("spokebio.release_check.run_disease_ontology_ingest")

    results = check_and_reingest(path)

    assert results["go"] == {"previous": "go-v0", "current": "go-v1", "changed": True, "totals": {"pathways_processed": 1}}
    assert results["reactome"]["changed"] is False
    assert results["disease_ontology"]["changed"] is False
    mock_go.assert_called_once_with(force_download=True)
    mock_reactome.assert_not_called()
    mock_do.assert_not_called()


def test_check_and_reingest_treats_a_first_run_as_changed(mocker, tmp_path):
    """No state file yet -- every source has no `previous`, which must not equal a
    real release string and so counts as changed, not silently skipped."""
    path = tmp_path / "state.json"
    _patch_releases(mocker)
    mocker.patch("spokebio.release_check.run_go_ingest", return_value={})
    mocker.patch("spokebio.release_check.run_reactome_ingest", return_value={})
    mocker.patch("spokebio.release_check.run_disease_ontology_ingest", return_value={})

    results = check_and_reingest(path)

    assert all(r["changed"] for r in results.values())
    assert all(r["previous"] is None for r in results.values())


def test_check_and_reingest_persists_the_new_state(mocker, tmp_path):
    path = tmp_path / "state.json"
    _patch_releases(mocker, go="go-v2")
    mocker.patch("spokebio.release_check.run_go_ingest", return_value={})
    mocker.patch("spokebio.release_check.run_reactome_ingest", return_value={})
    mocker.patch("spokebio.release_check.run_disease_ontology_ingest", return_value={})

    check_and_reingest(path)

    assert json.loads(path.read_text())["go"] == "go-v2"
