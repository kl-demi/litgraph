from datetime import UTC, datetime

from spokebio.ingest.pubtator import PubTatorClient, PubTatorExtractor, extract_mentions
from litgraph.graph.writer import CreateMissing
from spokebio.models import EntityMention
from spokebio.upsert import mark_papers_checked, upsert_mentions


def _annotation(entity_type, identifier, normalized_id, database, name, text, valid=True):
    return {
        "infons": {
            "type": entity_type,
            "identifier": identifier,
            "normalized_id": normalized_id,
            "database": database,
            "valid": valid,
            "name": name,
        },
        "text": text,
    }


_GENE = _annotation("Gene", "27161", 27161, "ncbi_gene", "AGO2", "Argonaute-2")
_CHEMICAL = _annotation("Chemical", "MESH:D000241", "D000241", "ncbi_mesh", "Adenosine", "adenosine")
_SPECIES = _annotation("Species", "9606", 9606, "ncbi_taxonomy", "9606", "human")
_DISEASE = _annotation("Disease", "MESH:C000719201", "C000719201", "ncbi_mesh", "Entomophobia", "insect")
_UNNORMALIZED = _annotation("Chemical", None, None, "ncbi_mesh", None, "indole glucosinolate", valid=False)


def test_extract_mentions_keeps_gene_chemical_species_disease():
    mentions = extract_mentions([_GENE, _CHEMICAL, _SPECIES, _DISEASE])

    by_type = {m.vertex_type: m for m in mentions}
    assert by_type["Gene"] == EntityMention(vertex_type="Gene", entity_id="ncbigene:27161", name="AGO2")
    assert by_type["Compound"] == EntityMention(vertex_type="Compound", entity_id="mesh:D000241", name="Adenosine")
    assert by_type["Organism"] == EntityMention(vertex_type="Organism", entity_id="9606", name="human")
    assert by_type["Disease"] == EntityMention(
        vertex_type="Disease", entity_id="mesh:C000719201", name="Entomophobia"
    )


def test_extract_mentions_drops_unnormalized():
    mentions = extract_mentions([_UNNORMALIZED])
    assert mentions == []


def test_extract_mentions_dedupes_within_document():
    mentions = extract_mentions([_GENE, _GENE])
    assert len(mentions) == 1


def test_extract_mentions_species_name_prefers_mention_text_over_infons_name():
    # infons["name"] for Species is just the taxon id again -- the mention text is the
    # only human-readable label PubTator gives for organisms.
    ann = _annotation("Species", "3702", 3702, "ncbi_taxonomy", "3702", "Arabidopsis thaliana")
    mentions = extract_mentions([ann])
    assert mentions[0].name == "Arabidopsis thaliana"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeHttpxClient:
    def __init__(self, docs_by_pmids):
        self._docs_by_pmids = docs_by_pmids
        self.get_calls = []

    def get(self, path, params=None):
        self.get_calls.append((path, params))
        requested = params["pmids"]
        return FakeResponse({"PubTator3": self._docs_by_pmids[requested]})


def _doc(pmid, annotations):
    return {"pmid": pmid, "passages": [{"annotations": annotations}]}


def test_fetch_mentions_batches_at_100_and_parses_annotations(mocker):
    fake_client = FakeHttpxClient({"111,222": [_doc(111, [_GENE]), _doc(222, [_SPECIES])]})
    mocker.patch("spokebio.ingest.pubtator.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    client = PubTatorClient()
    results = list(client.fetch_mentions(["111", "222"]))

    assert [pmid for pmid, _ in results] == ["111", "222"]
    gene_mentions = results[0][1]
    assert gene_mentions[0].entity_id == "ncbigene:27161"


def test_fetch_mentions_silently_skips_pmids_pubtator_has_no_doc_for(mocker):
    fake_client = FakeHttpxClient({"111,222": [_doc(111, [_GENE])]})
    mocker.patch("spokebio.ingest.pubtator.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    client = PubTatorClient()
    results = list(client.fetch_mentions(["111", "222"]))

    assert [pmid for pmid, _ in results] == ["111"]


def test_pubtator_extractor_maps_pmids_back_to_paper_ids(mocker):
    mocker.patch(
        "spokebio.ingest.pubtator.PubTatorClient.fetch_mentions",
        return_value=iter([("111", []), ("999", [])]),  # 999 was never asked for
    )

    results = list(PubTatorExtractor().extract([{"id": "pmid:111", "pmid": "111"}]))

    assert results == [("pmid:111", [])]


def test_upsert_mentions_writes_entities_and_edges_per_type(mocker):
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)
    mocker.patch("spokebio.upsert.run_write", return_value=[{"named": 0}])

    stats = upsert_mentions(
        {
            "pmid:111": [
                EntityMention(vertex_type="Gene", entity_id="ncbigene:27161", name="AGO2"),
                EntityMention(vertex_type="Organism", entity_id="9606", name="human"),
                EntityMention(vertex_type="Disease", entity_id="mesh:D003920", name="Diabetes Mellitus"),
            ]
        },
        source="pubtator3",
    )

    assert stats == {
        "new_organisms": 1,
        "new_genes": 1,
        "new_compounds": 0,
        "new_diseases": 1,
        "new_mention_edges": 3,
        "genes_named": 0,
    }
    # Compound has no mentions this batch, so it's skipped rather than issuing an empty call.
    assert [call.args[0] for call in mock_nodes.call_args_list] == ["Organism", "Gene", "Disease"]
    assert [call.kwargs["dst"] for call in mock_edges.call_args_list] == ["Organism", "Gene", "Disease"]


def test_upsert_mentions_never_overwrites_an_entity_name(mocker):
    """Another job may have named the node better, so an entity is only written on insert."""
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)
    mocker.patch("spokebio.upsert.upsert_edges", return_value=1)
    mocker.patch("spokebio.upsert.run_write", return_value=[{"named": 0}])

    upsert_mentions(
        {"pmid:111": [EntityMention(vertex_type="Gene", entity_id="ncbigene:1", name="A")]},
        source="pubtator3",
    )

    assert mock_nodes.call_args.kwargs["update_existing"] is False


def test_upsert_mentions_requires_both_endpoints_to_exist(mocker):
    """A paper missing from the graph isn't one this pass should invent."""
    mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)
    mocker.patch("spokebio.upsert.run_write", return_value=[{"named": 0}])

    upsert_mentions(
        {"pmid:111": [EntityMention(vertex_type="Gene", entity_id="ncbigene:1", name="A")]},
        source="pubtator3",
    )

    assert mock_edges.call_args.kwargs["create_missing"] is CreateMissing.NONE


def test_upsert_mentions_stamps_the_extractor_on_new_edges(mocker):
    """Set on creation only, so whichever extractor found a mention first keeps it."""
    mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)
    mock_edges = mocker.patch("spokebio.upsert.upsert_edges", return_value=1)
    mocker.patch("spokebio.upsert.run_write", return_value=[{"named": 0}])

    upsert_mentions(
        {"pmid:111": [EntityMention(vertex_type="Gene", entity_id="ncbigene:1", name="A")]},
        source="oryzabase-gazetteer",
    )

    assert mock_edges.call_args.args[1][0]["source"] == "oryzabase-gazetteer"
    assert mock_edges.call_args.kwargs["update_existing"] is False


def test_upsert_mentions_names_genes_the_pathway_loaders_left_bare(mocker):
    """Entity upserts write `name` only on insert, so a Gene the GAF or Oryzabase loader
    created key-only would keep a null name forever once a paper names it."""
    mocker.patch("spokebio.upsert.upsert_nodes", return_value=1)
    mocker.patch("spokebio.upsert.upsert_edges", return_value=1)
    mock_run_write = mocker.patch("spokebio.upsert.run_write", return_value=[{"named": 1}])

    stats = upsert_mentions(
        {
            "pmid:111": [
                EntityMention(vertex_type="Gene", entity_id="ncbigene:4340185", name="OsWRKY45"),
                # Organisms/Compounds are only ever created by this path, so they always
                # arrive named -- the backfill is Gene-only on purpose.
                EntityMention(vertex_type="Organism", entity_id="4530", name="rice"),
            ]
        },
        source="pubtator3",
    )

    assert stats["genes_named"] == 1
    assert mock_run_write.call_count == 1
    assert mock_run_write.call_args.kwargs["genes"] == [{"gene_id": "ncbigene:4340185", "name": "OsWRKY45"}]


def test_upsert_mentions_noop_on_empty(mocker):
    mock_nodes = mocker.patch("spokebio.upsert.upsert_nodes")
    stats = upsert_mentions({}, source="pubtator3")
    mock_nodes.assert_not_called()
    assert stats == {
        "new_organisms": 0,
        "new_genes": 0,
        "new_compounds": 0,
        "new_diseases": 0,
        "new_mention_edges": 0,
        "genes_named": 0,
    }


def test_mark_papers_checked_keys_rows_per_extractor(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    now = datetime(2026, 7, 21, tzinfo=UTC)

    mark_papers_checked("pubtator3", ["pmid:111", "pmid:222"], now)

    call = mock_run_write.call_args
    assert call.kwargs["rows"] == [
        {"check_id": "pubtator3:pmid:111", "paper_id": "pmid:111"},
        {"check_id": "pubtator3:pmid:222", "paper_id": "pmid:222"},
    ]
    assert call.kwargs["extractor"] == "pubtator3"
    assert call.kwargs["checked_at"] == now.isoformat()


def test_mark_papers_checked_noop_on_empty(mocker):
    mock_run_write = mocker.patch("spokebio.upsert.run_write")
    mark_papers_checked("pubtator3", [], datetime.now(UTC))
    mock_run_write.assert_not_called()
