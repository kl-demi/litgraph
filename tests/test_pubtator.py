from datetime import UTC, datetime

from plantbio.ingest.pubtator import PubTatorClient, extract_mentions
from plantbio.models import EntityMention
from plantbio.upsert import mark_papers_checked, upsert_mentions


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


def test_extract_mentions_keeps_gene_chemical_species():
    mentions = extract_mentions([_GENE, _CHEMICAL, _SPECIES])

    by_type = {m.vertex_type: m for m in mentions}
    assert by_type["Gene"] == EntityMention(vertex_type="Gene", entity_id="ncbigene:27161", name="AGO2")
    assert by_type["Compound"] == EntityMention(vertex_type="Compound", entity_id="mesh:D000241", name="Adenosine")
    assert by_type["Organism"] == EntityMention(vertex_type="Organism", entity_id="9606", name="human")


def test_extract_mentions_drops_disease_and_unnormalized():
    mentions = extract_mentions([_DISEASE, _UNNORMALIZED])
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
    mocker.patch("plantbio.ingest.pubtator.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    client = PubTatorClient()
    results = list(client.fetch_mentions(["111", "222"]))

    assert [pmid for pmid, _ in results] == ["111", "222"]
    gene_mentions = results[0][1]
    assert gene_mentions[0].entity_id == "ncbigene:27161"


def test_fetch_mentions_silently_skips_pmids_pubtator_has_no_doc_for(mocker):
    fake_client = FakeHttpxClient({"111,222": [_doc(111, [_GENE])]})
    mocker.patch("plantbio.ingest.pubtator.httpx.Client", return_value=fake_client)
    mocker.patch("time.sleep")

    client = PubTatorClient()
    results = list(client.fetch_mentions(["111", "222"]))

    assert [pmid for pmid, _ in results] == ["111"]


def test_upsert_mentions_writes_entities_and_edges_per_type(mocker):
    mock_run_script = mocker.patch("plantbio.upsert.arcadedb_http.run_script")
    mock_run_script.return_value = [{"value": 1}]

    paper_mentions = {
        "pmid:111": [
            EntityMention(vertex_type="Gene", entity_id="ncbigene:27161", name="AGO2"),
            EntityMention(vertex_type="Organism", entity_id="9606", name="human"),
        ]
    }

    stats = upsert_mentions(paper_mentions)

    assert stats == {"new_organisms": 1, "new_genes": 1, "new_compounds": 0, "new_mention_edges": 2}
    # 2 entity-upsert calls (Gene, Organism) + 2 edge-upsert calls -- Compound has no
    # mentions this batch, so it's skipped entirely rather than issuing an empty call.
    assert mock_run_script.call_count == 4


def test_upsert_mentions_noop_on_empty(mocker):
    mock_run_script = mocker.patch("plantbio.upsert.arcadedb_http.run_script")
    stats = upsert_mentions({})
    mock_run_script.assert_not_called()
    assert stats == {"new_organisms": 0, "new_genes": 0, "new_compounds": 0, "new_mention_edges": 0}


def test_mark_papers_checked_writes_merge(mocker):
    mock_run_write = mocker.patch("plantbio.upsert.run_write")
    now = datetime(2026, 7, 21, tzinfo=UTC)

    mark_papers_checked(["pmid:111", "pmid:222"], now)

    call = mock_run_write.call_args
    assert call.kwargs["paper_ids"] == ["pmid:111", "pmid:222"]
    assert call.kwargs["checked_at"] == now.isoformat()


def test_mark_papers_checked_noop_on_empty(mocker):
    mock_run_write = mocker.patch("plantbio.upsert.run_write")
    mark_papers_checked([], datetime.now(UTC))
    mock_run_write.assert_not_called()
