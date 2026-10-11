import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from travel_db.cli import main, process_inbox
from travel_db.models import ENTITY_ADAPTER
from travel_db.operations import OPERATION_ADAPTER
from travel_db.provenance import CONTROL_FIELDS, current_provenance
from travel_db.repository import ConflictError, DatabaseError
from travel_db.review import ReviewService, inspect_entity, pending_reviews
from travel_db.schema import dumps
from travel_db.service import IngestionService, validate_snapshot


def envelope(action="update", request="review-001"):
    return {
        "schema_version": "1.0",
        "request_id": request,
        "action": action,
        "reviewer": "가상 검토자",
        "reason": "가상 샘플 근거를 확인함",
    }


def all_evidence(entity):
    model = ENTITY_ADAPTER.validate_python(entity)
    data = model.model_dump(mode="json")
    return {
        f: [str(model.sources[0].url)]
        for f in type(model).model_fields
        if f not in CONTROL_FIELDS and data[f] is not None and data[f] != []
    }


@pytest.fixture
def loaded(repository, sample):
    IngestionService(repository).ingest(sample, dry_run=False)
    target_id = repository.tables["restaurants"][0]["id"]
    return repository, target_id, deepcopy(sample["items"][-1])


def update(loaded, amount="1200", request="review-001"):
    repository, target_id, original = loaded
    entity = deepcopy(original)
    entity["price"]["amount"] = amount
    return {
        **envelope(request=request),
        "target_id": target_id,
        "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
        "entity": entity,
        "field_sources": {"price": [entity["sources"][0]["url"]]},
    }


@pytest.fixture
def quarantined(repository, sample):
    correct = deepcopy(sample["items"][-1])
    sample["items"][-1]["price"]["currency"] = "INVALID"
    IngestionService(repository).ingest(sample, dry_run=False)
    queue_id = repository.tables["verification_queue"][0]["id"]
    return repository, queue_id, correct


def approve(quarantined):
    _, queue_id, entity = quarantined
    return {
        **envelope("approve"),
        "queue_id": queue_id,
        "mode": "create",
        "entity": deepcopy(entity),
        "field_sources": all_evidence(entity),
    }


def test_update_diff_is_dry_run_and_apply_preserves_identity(loaded):
    repository, target_id, _ = loaded
    before = deepcopy(repository.tables["restaurants"][0])
    payload = update(loaded)
    service = ReviewService(repository)
    snapshot = repository.read().digest
    report = service.execute(payload)
    assert report["status"] == "updated" and not report["committed"]
    assert set(report["changes"]) == {"price"}
    assert repository.read().digest == snapshot
    applied = service.execute(payload, dry_run=False)
    after = repository.tables["restaurants"][0]
    assert after["id"] == target_id and after["created_at"] == before["created_at"]
    assert after["updated_at"] != before["updated_at"]
    assert json.loads(after["price"])["amount"] == "1200"
    assert inspect_entity(repository, target_id)["expected_hash"] == applied["expected_hash_after"]
    history = repository.tables["change_history"][-1]
    assert json.loads(history["before_json"])["entity"] == before
    assert json.loads(history["after_json"])["entity"] == after
    assert json.loads(history["after_json"])["reviewer"] == payload["reviewer"]


def test_operation_replay_and_request_conflict(loaded):
    repository, _, _ = loaded
    payload = update(loaded)
    service = ReviewService(repository)
    service.execute(payload, dry_run=False)
    snapshot = repository.read().digest
    replay = service.execute(payload, dry_run=False)
    assert replay["replayed"] and replay["committed"]
    assert repository.read().digest == snapshot
    payload["reason"] = "다른 검토 내용"
    with pytest.raises(ConflictError, match="request_id"):
        service.execute(payload, dry_run=False)


def test_stale_update_hash_aborts(loaded):
    repository, _, _ = loaded
    stale = update(loaded, amount="1400", request="stale")
    ReviewService(repository).execute(update(loaded), dry_run=False)
    snapshot = repository.read().digest
    with pytest.raises(ConflictError, match="검토 이후"):
        ReviewService(repository).execute(stale, dry_run=False)
    assert repository.read().digest == snapshot


@pytest.mark.parametrize("change", ["missing", "unknown", "empty", "external"])
def test_bad_field_sources_abort(loaded, change):
    repository, _, _ = loaded
    payload = update(loaded)
    if change == "missing":
        payload["field_sources"] = {}
    elif change == "unknown":
        payload["field_sources"]["hours"] = payload["field_sources"]["price"]
    elif change == "empty":
        payload["field_sources"]["price"] = []
    else:
        payload["field_sources"]["price"] = ["https://example.com/not-registered"]
    snapshot = repository.read().digest
    with pytest.raises(ValueError):
        ReviewService(repository).execute(payload, dry_run=False)
    assert repository.read().digest == snapshot


def test_source_replacement_requires_new_evidence_even_for_unchanged_fields(loaded):
    repository, target_id, _ = loaded
    service = ReviewService(repository)
    service.execute(update(loaded), dry_run=False)
    payload = update(loaded, amount="1200", request="replace-source")
    payload["entity"]["sources"][0]["url"] = "https://example.com/new-evidence"
    payload["field_sources"] = {}
    with pytest.raises(ValueError, match="price"):
        service.execute(payload)
    payload["field_sources"] = {"price": ["https://example.com/new-evidence"]}
    service.execute(payload, dry_run=False)
    inspection = inspect_entity(repository, target_id)
    assert len(repository.tables["sources"]) == 4
    assert inspection["field_sources"]["price"]["source_ids"] != json.loads(
        repository.tables["field_sources"][0]["source_ids"]
    )


@pytest.mark.parametrize("change", ["target", "id", "kind", "reference", "unverified"])
def test_bad_update_target_or_entity_aborts(loaded, change):
    repository, _, _ = loaded
    payload = update(loaded)
    if change == "target":
        payload["target_id"] = "absent"
    elif change == "id":
        payload["entity"]["id"] = "other-id"
    elif change == "kind":
        payload["entity"]["kind"] = "experience"
        payload["entity"]["experience_type"] = "cafe"
    elif change == "reference":
        payload["entity"]["region_id"] = "missing-region"
        payload["field_sources"]["region_id"] = payload["field_sources"]["price"]
    else:
        payload["entity"]["verification_status"] = "unverified"
    snapshot = repository.read().digest
    with pytest.raises((DatabaseError, ValueError)):
        ReviewService(repository).execute(payload, dry_run=False)
    assert repository.read().digest == snapshot


def test_update_validates_dependents_not_only_changed_entity(loaded, sample):
    repository, _, _ = loaded
    payload = {
        **envelope(),
        "target_id": "sample-country",
        "expected_hash": inspect_entity(repository, "sample-country")["expected_hash"],
        "entity": deepcopy(sample["items"][0]),
        "field_sources": {"country_code": [sample["items"][0]["sources"][0]["url"]]},
    }
    payload["entity"]["country_code"] = "KR"
    snapshot = repository.read().digest
    with pytest.raises(DatabaseError, match="손상"):
        ReviewService(repository).execute(payload, dry_run=False)
    assert repository.read().digest == snapshot


def test_noop_update_does_not_change_revision(loaded):
    repository, target_id, original = loaded
    payload = {
        **envelope(),
        "target_id": target_id,
        "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
        "entity": original,
    }
    before = deepcopy(repository.tables["restaurants"])
    report = ReviewService(repository).execute(payload, dry_run=False)
    assert report["status"] == "unchanged"
    assert repository.tables["restaurants"] == before
    assert repository.tables["field_sources"] == []


def test_corrected_approval_resolves_without_erasing_quarantine(quarantined):
    repository, _, _ = quarantined
    before_queue = deepcopy(repository.tables["verification_queue"])
    payload = approve(quarantined)
    service = ReviewService(repository)
    dry = service.execute(payload)
    assert dry["status"] == "created" and not dry["committed"]
    assert pending_reviews(repository)["pending"] == 1
    report = service.execute(payload, dry_run=False)
    assert report["status"] == "created"
    assert pending_reviews(repository)["pending"] == 0
    assert repository.tables["verification_queue"] == before_queue
    assert len(repository.tables["review_decisions"]) == 1
    assert set(inspect_entity(repository, report["id"])["field_sources"]) == set(
        payload["field_sources"]
    )
    assert service.execute(payload, dry_run=False)["replayed"]
    payload["request_id"] = "second-approve"
    with pytest.raises(ConflictError, match="이미"):
        service.execute(payload, dry_run=False)


def test_rejection_is_audited_idempotent_and_dry_run(quarantined):
    repository, queue_id, _ = quarantined
    payload = {**envelope("reject"), "queue_id": queue_id}
    before = repository.read().digest
    service = ReviewService(repository)
    assert service.execute(payload)["status"] == "rejected"
    assert repository.read().digest == before
    service.execute(payload, dry_run=False)
    assert repository.tables["restaurants"] == []
    assert repository.tables["review_decisions"][0]["decision"] == "reject"
    assert pending_reviews(repository)["pending"] == 0
    assert service.execute(payload, dry_run=False)["replayed"]
    other = approve(quarantined)
    other["request_id"] = "approve-after-reject"
    with pytest.raises(ConflictError, match="이미"):
        service.execute(other, dry_run=False)


def test_missing_queue_does_not_create_decision(repository):
    with pytest.raises(DatabaseError):
        ReviewService(repository).execute({**envelope("reject"), "queue_id": "missing"})
    assert repository.commits == 0


def test_change_candidate_approval_updates_explicit_target(loaded):
    repository, target_id, _ = loaded
    payload = update(loaded)
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "queue-change", "items": [payload["entity"]]},
        dry_run=False,
    )
    queue_id = repository.tables["verification_queue"][0]["id"]
    payload.update(action="approve", queue_id=queue_id, mode="update")
    report = ReviewService(repository).execute(payload, dry_run=False)
    assert report["id"] == target_id and report["status"] == "updated"
    assert len(repository.tables["restaurants"]) == 1
    assert pending_reviews(repository)["pending"] == 0


def test_change_candidate_cannot_be_approved_as_new(loaded):
    repository, _, _ = loaded
    payload = update(loaded)
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "queue-change", "items": [payload["entity"]]},
        dry_run=False,
    )
    payload.update(
        action="approve", mode="create", queue_id=repository.tables["verification_queue"][0]["id"]
    )
    payload.pop("target_id")
    payload.pop("expected_hash")
    with pytest.raises(ConflictError, match="변경 후보"):
        ReviewService(repository).execute(payload)


def test_fuzzy_new_approval_requires_explicit_distinct_acknowledgement(loaded):
    repository, target_id, original = loaded
    entity = deepcopy(original)
    entity.update(id="distinct-place", name="샘플 식당 (가상) 2")
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "duplicate-review", "items": [entity]},
        dry_run=False,
    )
    queue = repository.tables["verification_queue"][0]
    assert queue["reason"] == "duplicate_candidate"
    payload = {
        **envelope("approve"),
        "queue_id": queue["id"],
        "mode": "create",
        "entity": entity,
        "field_sources": all_evidence(entity),
    }
    with pytest.raises(ConflictError, match="distinct_from"):
        ReviewService(repository).execute(payload)
    payload["distinct_from"] = [target_id]
    result = ReviewService(repository).execute(payload, dry_run=False)
    assert result["status"] == "created" and result["distinct_from"] == [target_id]
    audit = json.loads(repository.tables["change_history"][-1]["after_json"])
    assert audit["distinct_from"] == [target_id]
    assert len(repository.tables["restaurants"]) == 2


def test_exact_duplicate_cannot_be_approved_as_new(loaded):
    repository, _, original = loaded
    entity = deepcopy(original)
    entity["id"] = "different-id"
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "duplicate-review", "items": [entity]},
        dry_run=False,
    )
    payload = {
        **envelope("approve"),
        "queue_id": repository.tables["verification_queue"][0]["id"],
        "mode": "create",
        "entity": entity,
        "field_sources": all_evidence(entity),
    }
    with pytest.raises(ConflictError, match="식별키"):
        ReviewService(repository).execute(payload)


def test_restore_previous_values_and_evidence_with_new_audit(loaded):
    repository, target_id, _ = loaded
    service = ReviewService(repository)
    first = service.execute(update(loaded), dry_run=False)
    before_second = inspect_entity(repository, target_id)
    second = service.execute(update(loaded, "1400", "review-002"), dry_run=False)
    payload = {
        **envelope("restore", "restore-001"),
        "history_id": second["history_id"],
        "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
    }
    snapshot = repository.read().digest
    assert service.execute(payload)["status"] == "restored"
    assert repository.read().digest == snapshot
    restored = service.execute(payload, dry_run=False)
    now = inspect_entity(repository, target_id)
    assert json.loads(now["entity"]["price"])["amount"] == "1200"
    assert now["field_sources"] == before_second["field_sources"]
    assert now["entity"]["created_at"] == before_second["entity"]["created_at"]
    assert now["entity"]["updated_at"] != before_second["entity"]["updated_at"]
    assert service.execute(payload, dry_run=False)["replayed"]
    # A restoration is itself reversible by restoring its before snapshot.
    undo = {
        **envelope("restore", "restore-002"),
        "history_id": restored["history_id"],
        "expected_hash": now["expected_hash"],
    }
    service.execute(undo, dry_run=False)
    assert json.loads(repository.tables["restaurants"][0]["price"])["amount"] == "1400"
    assert first["history_id"] != second["history_id"]


def test_restore_legacy_update_retains_unknown_field_provenance(loaded):
    repository, target_id, _ = loaded
    before = deepcopy(repository.tables["restaurants"][0])
    report = ReviewService(repository).execute(update(loaded), dry_run=False)
    payload = {
        **envelope("restore", "restore-001"),
        "history_id": report["history_id"],
        "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
    }
    ReviewService(repository).execute(payload, dry_run=False)
    assert repository.tables["restaurants"][0]["price"] == before["price"]
    assert current_provenance(repository.read(), repository.tables["restaurants"][0]) == {}


def test_restore_stops_after_later_changes_even_with_current_hash(loaded):
    repository, target_id, _ = loaded
    service = ReviewService(repository)
    first = service.execute(update(loaded), dry_run=False)
    service.execute(update(loaded, "1400", "review-002"), dry_run=False)
    before = repository.read().digest
    with pytest.raises(ConflictError, match="이력 이후"):
        service.execute(
            {
                **envelope("restore", "restore-after-change"),
                "history_id": first["history_id"],
                "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
            }
        )
    assert repository.read().digest == before


def test_create_history_cannot_be_restored_by_deleting_rows(loaded):
    repository, target_id, _ = loaded
    with pytest.raises(ValueError, match="update/restore"):
        ReviewService(repository).execute(
            {
                **envelope("restore"),
                "history_id": repository.tables["change_history"][-1]["id"],
                "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
            }
        )


@pytest.mark.parametrize("bad_before", [None, [], {}, {"entity": {}, "field_sources": {}}])
def test_corrupt_restore_history_aborts(loaded, bad_before):
    repository, target_id, _ = loaded
    service = ReviewService(repository)
    report = service.execute(update(loaded), dry_run=False)
    repository.tables["change_history"][-1]["before_json"] = dumps(bad_before)
    before = repository.read().digest
    with pytest.raises(DatabaseError, match="손상"):
        service.execute(
            {
                **envelope("restore", "restore-001"),
                "history_id": report["history_id"],
                "expected_hash": inspect_entity(repository, target_id)["expected_hash"],
            }
        )
    assert repository.read().digest == before


@pytest.mark.parametrize("action", ["update", "approve", "reject"])
def test_atomic_failure_leaves_entity_queue_evidence_and_receipt_unchanged(loaded, action):
    repository, _, original = loaded
    if action == "update":
        payload = update(loaded)
    else:
        invalid = deepcopy(original)
        invalid["verification_status"] = "unverified"
        IngestionService(repository).ingest(
            {"schema_version": "1.0", "request_id": "queue-invalid", "items": [invalid]},
            dry_run=False,
        )
        queue_id = repository.tables["verification_queue"][0]["id"]
        payload = {**envelope("reject"), "queue_id": queue_id}
        if action == "approve":
            payload = {
                **update(loaded),
                "action": "approve",
                "mode": "update",
                "queue_id": queue_id,
            }
    repository.fail_commit = True
    snapshot = repository.read().digest
    with pytest.raises(DatabaseError):
        ReviewService(repository).execute(payload, dry_run=False)
    assert repository.read().digest == snapshot


def test_personal_rating_evidence_is_explicit_and_has_no_fabricated_url(loaded):
    repository, target_id, original = loaded
    entity = {k: v for k, v in original.items() if k not in {"address", "price"}}
    entity.update(
        id="personal-rating",
        kind="rating",
        target_id=target_id,
        visited=True,
        satisfaction=4,
        sources=[],
        verification_status="unverified",
    )
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "queue-rating", "items": [entity]}, dry_run=False
    )
    entity["verification_status"] = "verified"
    payload = {
        **envelope("approve"),
        "mode": "create",
        "queue_id": repository.tables["verification_queue"][0]["id"],
        "entity": entity,
        "field_sources": {
            "region_id": [original["sources"][0]["url"]],
            "name": [original["sources"][0]["url"]],
            "target_id": [original["sources"][0]["url"]],
            "visited": [],
            "satisfaction": [],
        },
    }
    payload["entity"]["sources"] = original["sources"]
    report = ReviewService(repository).execute(payload, dry_run=False)
    provenance = inspect_entity(repository, report["id"])["field_sources"]
    assert provenance["satisfaction"]["basis"] == "personal"
    assert provenance["satisfaction"]["source_ids"] == []


def test_corrupt_evidence_and_decisions_stop_writes(quarantined):
    repository, _, _ = quarantined
    report = ReviewService(repository).execute(approve(quarantined), dry_run=False)
    original = deepcopy(repository.tables["field_sources"])
    repository.tables["field_sources"][0]["source_ids"] = '["missing"]'
    with pytest.raises(DatabaseError, match="손상"):
        validate_snapshot(repository.read())
    repository.tables["field_sources"] = original
    repository.tables["review_decisions"][0]["entity_id"] = "missing"
    with pytest.raises(DatabaseError, match="손상"):
        inspect_entity(repository, report["id"])


def test_json_schema_matches_operation_contract():
    path = Path(__file__).parents[1] / "docs/operation.schema.json"
    assert json.loads(path.read_text()) == OPERATION_ADAPTER.json_schema()


def test_operation_cli_validation_needs_no_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    path = tmp_path / "operation.json"
    path.write_text(dumps({**envelope("reject"), "queue_id": "sample-queue"}))
    assert main(["validate-operation", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["validation_mode"] == "input_only"


def test_operate_cli_default_dry_run_and_inspect(monkeypatch, tmp_path, capsys, loaded):
    repository, target_id, _ = loaded
    repository.spreadsheet_id = "fictional-stage3-cli"
    monkeypatch.setattr("travel_db.cli.SheetsRepository.from_env", lambda: repository)
    path = tmp_path / "operation.json"
    path.write_text(dumps(update(loaded)))
    before = repository.read().digest
    assert main(["operate", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert repository.read().digest == before
    assert main(["operate", str(path), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["committed"]
    assert main(["inspect", target_id]) == 0
    assert json.loads(capsys.readouterr().out)["expected_hash"]


def test_operation_model_enforces_update_target_and_hash(sample):
    payload = {
        **envelope("approve"),
        "mode": "update",
        "queue_id": "queue",
        "entity": sample["items"][-1],
    }
    with pytest.raises(ValidationError):
        OPERATION_ADAPTER.validate_python(payload)


def test_concurrent_commit_aborts_all_review_records(loaded, monkeypatch):
    repository, _, _ = loaded
    payload = update(loaded)
    original_commit = repository.commit

    def competing_commit(additions, expected, **kwargs):
        repository.tables["restaurants"][0]["address"] = "다른 작업자가 변경한 가상 주소"
        original_commit(additions, expected, **kwargs)

    monkeypatch.setattr(repository, "commit", competing_commit)
    with pytest.raises(ConflictError):
        ReviewService(repository).execute(payload, dry_run=False)
    assert repository.tables["field_sources"] == []
    assert len(repository.tables["change_history"]) == 3
    assert len(repository.tables["ingestion_runs"]) == 1


def test_uncertain_response_is_recovered_from_receipt_without_second_update(loaded, monkeypatch):
    repository, _, _ = loaded
    payload = update(loaded)
    original_commit = repository.commit

    def lost_response(additions, expected, **kwargs):
        original_commit(additions, expected, **kwargs)
        raise DatabaseError("가상 응답 유실: 결과 불확실")

    monkeypatch.setattr(repository, "commit", lost_response)
    service = ReviewService(repository)
    with pytest.raises(DatabaseError, match="불확실"):
        service.execute(payload, dry_run=False)
    committed = repository.commits
    assert service.execute(payload, dry_run=False)["replayed"]
    assert repository.commits == committed


def test_inbox_routes_operations_and_replays_without_duplicate_decisions(quarantined):
    repository, queue_id, _ = quarantined
    payload = {**envelope("reject"), "queue_id": queue_id}
    repository.tables["inbox"].append(
        {
            "request_id": payload["request_id"],
            "payload_json": dumps(payload),
            "submitted_at": "2026-01-01T00:00:00Z",
        }
    )
    before = repository.read().digest
    assert process_inbox(repository)["requests"][0]["status"] == "rejected"
    assert repository.read().digest == before
    assert process_inbox(repository, dry_run=False)["requests"][0]["committed"]
    assert process_inbox(repository, dry_run=False)["requests"][0]["replayed"]
    assert len(repository.tables["review_decisions"]) == 1


def test_inbox_invalid_operation_is_reported_without_partial_write(repository):
    payload = {**envelope("update"), "target_id": "missing"}
    repository.tables["inbox"].append(
        {"request_id": payload["request_id"], "payload_json": dumps(payload), "submitted_at": ""}
    )
    report = process_inbox(repository, dry_run=False)
    assert "error" in report["requests"][0]
    assert repository.commits == 0


def test_repeat_ingestion_after_update_skips_instead_of_duplicate(loaded):
    repository, _, _ = loaded
    payload = update(loaded)
    ReviewService(repository).execute(payload, dry_run=False)
    report = IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "fresh-research", "items": [payload["entity"]]},
        dry_run=False,
    )
    assert report["skipped"] == 1 and report["created"] == 0
    assert len(repository.tables["restaurants"]) == 1


def test_setting_unknown_value_null_requires_explicit_evidence(loaded):
    repository, target_id, original = loaded
    payload = update(loaded)
    payload["entity"] = deepcopy(original)
    payload["entity"]["price"] = None
    report = ReviewService(repository).execute(payload, dry_run=False)
    assert report["changes"]["price"]["after"] == ""
    assert repository.tables["restaurants"][0]["price"] == ""
    assert "price" in inspect_entity(repository, target_id)["field_sources"]


def test_provenance_only_change_is_audited_and_restorable(loaded):
    repository, target_id, original = loaded
    payload = update(loaded, "1000")
    report = ReviewService(repository).execute(payload, dry_run=False)
    assert report["status"] == "updated" and report["changes"] == {}
    assert "price" in inspect_entity(repository, target_id)["field_sources"]
    ReviewService(repository).execute(
        {
            **envelope("restore", "restore-evidence"),
            "history_id": report["history_id"],
            "expected_hash": report["expected_hash_after"],
        },
        dry_run=False,
    )
    assert inspect_entity(repository, target_id)["field_sources"] == {}
    assert json.loads(repository.tables["restaurants"][0]["price"]) == original["price"]


def test_duplicate_resolution_record_is_corruption(quarantined):
    repository, queue_id, _ = quarantined
    ReviewService(repository).execute({**envelope("reject"), "queue_id": queue_id}, dry_run=False)
    duplicate = deepcopy(repository.tables["review_decisions"][0])
    duplicate["id"] = "other-decision-id"
    repository.tables["review_decisions"].append(duplicate)
    with pytest.raises(DatabaseError, match="손상"):
        pending_reviews(repository)


def test_restore_checks_new_dependent_regions(repository, sample):
    country = deepcopy(sample["items"][0])
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "country-only", "items": [country]}, dry_run=False
    )
    country["country_code"] = "KR"
    payload = {
        **envelope(),
        "target_id": country["id"],
        "expected_hash": inspect_entity(repository, country["id"])["expected_hash"],
        "entity": country,
        "field_sources": {"country_code": [country["sources"][0]["url"]]},
    }
    report = ReviewService(repository).execute(payload, dry_run=False)
    city = deepcopy(sample["items"][1])
    city["country_code"] = "KR"
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "new-city", "items": [city]}, dry_run=False
    )
    before = repository.read().digest
    with pytest.raises(DatabaseError, match="손상"):
        ReviewService(repository).execute(
            {
                **envelope("restore", "restore-country"),
                "history_id": report["history_id"],
                "expected_hash": report["expected_hash_after"],
            },
            dry_run=False,
        )
    assert repository.read().digest == before


def test_restore_rejects_identity_now_used_by_another_entity(loaded):
    repository, _, original = loaded
    payload = update(loaded, "1000")
    payload["entity"]["name"] = "완전히 다른 가상 이름 XYZ"
    payload["field_sources"] = {"name": [original["sources"][0]["url"]]}
    report = ReviewService(repository).execute(payload, dry_run=False)
    formerly_named = {**deepcopy(original), "id": "different-explicit-identity"}
    added = IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "reused-name", "items": [formerly_named]},
        dry_run=False,
    )
    assert added["created"] == 1
    before = repository.read().digest
    with pytest.raises(ConflictError, match="식별키"):
        ReviewService(repository).execute(
            {
                **envelope("restore", "restore-name"),
                "history_id": report["history_id"],
                "expected_hash": report["expected_hash_after"],
            },
            dry_run=False,
        )
    assert repository.read().digest == before
