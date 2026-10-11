import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from travel_db.cli import process_inbox
from travel_db.repository import ConflictError, DatabaseError
from travel_db.schema import dumps
from travel_db.service import IngestionService, validate_snapshot


def test_dry_run_does_not_write(repository, sample):
    before = repository.read().digest
    report = IngestionService(repository).ingest(sample)
    assert report["created"] == 3
    assert report["dry_run"] is True and report["committed"] is False
    assert repository.read().digest == before
    assert repository.commits == 0


def test_create_read_and_same_request_replay(repository, sample):
    service = IngestionService(repository)
    first = service.ingest(sample, dry_run=False)
    snapshot = repository.read()
    entities, _ = validate_snapshot(snapshot)
    assert len(entities) == 3
    assert len(snapshot.tables["sources"]) == 3
    assert len(snapshot.tables["change_history"]) == 3
    assert first["committed"] is True
    second = service.ingest(sample, dry_run=False)
    assert second["replayed"] is True
    assert repository.commits == 1
    assert repository.read().digest == snapshot.digest


def test_same_data_new_request_skips_without_duplicate_entities(repository, sample):
    service = IngestionService(repository)
    service.ingest(sample, dry_run=False)
    sample["request_id"] = "sample-repeat"
    report = service.ingest(sample, dry_run=False)
    assert (report["created"], report["skipped"], report["queued"]) == (0, 3, 0)
    assert len(repository.tables["restaurants"]) == 1
    assert len(repository.tables["sources"]) == 3
    assert len(repository.tables["change_history"]) == 3


def test_request_id_cannot_be_reused_for_different_payload(repository, sample):
    service = IngestionService(repository)
    service.ingest(sample, dry_run=False)
    before = repository.read().digest
    sample["items"][-1]["name"] = "다른 샘플"
    with pytest.raises(ConflictError, match="request_id"):
        service.ingest(sample, dry_run=False)
    assert repository.read().digest == before


def test_duplicate_in_one_batch_is_skipped(repository, sample):
    sample["items"].append(deepcopy(sample["items"][-1]))
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["created"] == 3 and report["skipped"] == 1
    assert len(repository.tables["restaurants"]) == 1


def test_explicit_different_id_with_same_identity_is_quarantined(repository, sample):
    service = IngestionService(repository)
    service.ingest(sample, dry_run=False)
    sample["request_id"] = "sample-explicit-duplicate"
    sample["items"][-1]["id"] = "other-restaurant"
    report = service.ingest(sample, dry_run=False)
    assert report["queued"] == 1
    assert report["results"][-1]["reason"] == "duplicate_candidate"
    assert len(repository.tables["restaurants"]) == 1


def test_invalid_currency_is_separate_and_raw_is_preserved(repository, sample):
    invalid = sample["items"][-1]
    invalid["price"]["currency"] = "NOT_A_CURRENCY"
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["created"] == 2 and report["queued"] == 1
    assert repository.tables["restaurants"] == []
    queued = repository.tables["verification_queue"][0]
    assert queued["reason"] == "validation_error"
    assert json.loads(queued["payload_json"]) == invalid
    assert json.loads(queued["errors_json"])
    assert len(repository.tables["change_history"]) == 3


def test_unverified_fact_is_quarantined(repository, sample):
    item = sample["items"][-1]
    item["verification_status"] = "unverified"
    item["last_verified_on"] = None
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["queued"] == 1
    assert report["results"][-1]["reason"] == "needs_verification"


@pytest.mark.parametrize(
    "field,value", [("region_id", "missing-region"), ("signature_food_ids", ["sample-city"])]
)
def test_missing_or_wrong_kind_reference_is_quarantined(repository, sample, field, value):
    sample["items"][-1][field] = value
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["results"][-1]["reason"] == "reference_error"
    assert repository.tables["restaurants"] == []


def test_regions_processed_by_hierarchy_despite_reverse_input(repository, sample):
    sample["items"].reverse()
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["created"] == 3 and report["queued"] == 0
    assert [r["item_index"] for r in report["results"]] == [0, 1, 2]


def test_region_country_mismatch_is_quarantined(repository, sample):
    sample["items"][1]["country_code"] = "KR"
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["created"] == 1
    assert report["queued"] == 2  # city invalid; dependent restaurant also invalid


def test_existing_changes_report_diff_without_overwrite(repository, sample):
    service = IngestionService(repository)
    service.ingest(sample, dry_run=False)
    original = deepcopy(repository.tables["restaurants"])
    sample["request_id"] = "sample-changed-price"
    sample["items"][-1]["price"]["amount"] = "1200"
    report = service.ingest(sample, dry_run=False)
    assert report["queued"] == 1 and report["skipped"] == 2
    result = report["results"][-1]
    assert result["reason"] == "change_candidate"
    assert "price" in result["changes"]
    assert repository.tables["restaurants"] == original
    history = repository.tables["change_history"][-1]
    assert json.loads(history["before_json"])["price"] == original[0]["price"]


def test_commit_failure_leaves_no_partial_data(repository, sample):
    repository.fail_commit = True
    before = repository.read().digest
    with pytest.raises(DatabaseError):
        IngestionService(repository).ingest(sample, dry_run=False)
    assert repository.read().digest == before


def test_corrupt_existing_data_aborts_instead_of_overwrite(repository, sample):
    service = IngestionService(repository)
    service.ingest(sample, dry_run=False)
    repository.tables["restaurants"][0]["region_id"] = "missing-region"
    before = repository.read().digest
    sample["request_id"] = "sample-corrupt"
    with pytest.raises(DatabaseError, match="손상"):
        service.ingest(sample, dry_run=False)
    assert repository.read().digest == before


def test_invalid_envelope_is_rejected_without_writes(repository):
    with pytest.raises(ValidationError):
        IngestionService(repository).ingest({"schema_version": "9", "request_id": "x", "items": []})
    assert repository.commits == 0


def test_inbox_write_then_replay_with_matching_run_id(repository, sample):
    repository.tables["inbox"].append(
        {
            "request_id": sample["request_id"],
            "payload_json": dumps(sample),
            "submitted_at": "2026-01-01T00:00:00Z",
        }
    )
    dry = process_inbox(repository)
    assert dry["requests"][0]["created"] == 3 and repository.commits == 0
    first = process_inbox(repository, dry_run=False)
    assert first["requests"][0]["committed"]
    second = process_inbox(repository, dry_run=False)
    assert second["requests"][0]["replayed"]
    assert repository.commits == 1


def test_inbox_request_mismatch_is_rejected(repository, sample):
    repository.tables["inbox"].append(
        {"request_id": "different", "payload_json": dumps(sample), "submitted_at": ""}
    )
    report = process_inbox(repository, dry_run=False)
    assert "error" in report["requests"][0]
    assert repository.commits == 0


def test_operational_id_collision_is_detected_in_dry_run(repository, sample):
    sample["items"][0]["id"] = sample["request_id"]
    report = IngestionService(repository).ingest(sample)
    assert report["results"][0]["reason"] == "id_conflict"
    assert repository.commits == 0


def test_all_entity_kinds_and_references_roundtrip(repository, sample):
    base = sample["items"][-1]
    food = {k: deepcopy(v) for k, v in base.items() if k not in {"address", "price"}}
    food.update(
        {
            "id": "sample-food",
            "kind": "food",
            "name": "샘플 음식",
            "food_type": "샘플",
            "representative_region_ids": ["sample-city"],
        }
    )
    base["id"] = "sample-restaurant"
    base["signature_food_ids"] = ["sample-food"]
    experience = deepcopy(base)
    for field in ("signature_food_ids",):
        experience.pop(field)
    experience.update({"id": "sample-cafe", "kind": "experience", "experience_type": "cafe"})
    transport = deepcopy(experience)
    transport.pop("experience_type")
    transport.update(
        {
            "id": "sample-route",
            "kind": "transport",
            "transport_type": "route",
            "origin_region_id": "sample-city",
            "destination_region_id": "sample-city",
        }
    )
    budget = {
        k: deepcopy(v)
        for k, v in food.items()
        if k not in {"food_type", "representative_region_ids"}
    }
    budget.update(
        {
            "id": "sample-budget",
            "kind": "budget",
            "budget_type": "food",
            "price": None,
            "people": 1,
            "duration_days": 2,
        }
    )
    rating = {
        k: deepcopy(v)
        for k, v in budget.items()
        if k not in {"budget_type", "price", "people", "duration_days"}
    }
    rating.update(
        {
            "id": "sample-rating",
            "kind": "rating",
            "target_id": "sample-restaurant",
            "sources": [],
            "visited": True,
            "satisfaction": 4,
            "taste_fit": 5,
        }
    )
    sample["items"].extend([food, experience, transport, budget, rating])
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["queued"] == 0
    assert report["created"] == 8
    entities, _ = validate_snapshot(repository.read())
    assert entities["sample-rating"].satisfaction == 4
    assert entities["sample-food"].representative_region_ids == ["sample-city"]
