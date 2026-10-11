import json
from copy import deepcopy
from pathlib import Path

import pytest

from travel_db.audit import audit_database
from travel_db.cli import main
from travel_db.live import run_e2e
from travel_db.schema import dumps
from travel_db.service import IngestionService


@pytest.fixture
def seeded(repository, sample):
    IngestionService(repository).ingest(sample, dry_run=False)
    return repository


def inbox_row(payload):
    return {"request_id": payload["request_id"], "payload_json": dumps(payload), "submitted_at": ""}


def test_empty_and_populated_audit_does_not_write(repository, seeded):
    before = seeded.read().digest
    result = audit_database(seeded)
    assert result["healthy"] and result["read_only"] and result["issue_count"] == 0
    assert result["row_counts"]["restaurants"] == 1
    assert result["verification_queue"]["pending"] == 0
    assert result["snapshot_digest"] == before == seeded.read().digest
    assert seeded.commits == 1
    assert audit_database(type(repository)())["healthy"]


def test_audit_reports_inbox_receipts_and_envelope_warnings(seeded, sample):
    seeded.tables["inbox"].append(inbox_row(sample))
    unprocessed = {**sample, "request_id": "pending-inbox"}
    seeded.tables["inbox"].append(inbox_row(unprocessed))
    seeded.tables["inbox"].append(
        {"request_id": "bad-inbox", "payload_json": "private invalid raw", "submitted_at": ""}
    )
    result = audit_database(seeded)
    assert result["inbox"] == {"processed": 1, "unprocessed": 1, "invalid": 1, "conflict": 0}
    assert result["healthy"] and result["warning_count"] == 1
    assert "private invalid raw" not in dumps(result)


def test_changed_processed_inbox_is_error(seeded, sample):
    altered = deepcopy(sample)
    altered["items"][2]["name"] = "다른 입력"
    seeded.tables["inbox"].append(inbox_row(altered))
    result = audit_database(seeded)
    assert not result["healthy"] and result["inbox"]["conflict"] == 1
    assert result["issues"][0]["code"] == "request_payload_conflict"


def test_item_validation_is_left_to_ingestion(seeded, sample):
    payload = {**sample, "request_id": "bad-item", "items": [{"kind": "unknown"}]}
    seeded.tables["inbox"].append(inbox_row(payload))
    result = audit_database(seeded)
    assert result["healthy"] and result["inbox"]["unprocessed"] == 1


@pytest.mark.parametrize("valid", [True, False])
def test_operation_inbox_is_only_validated_not_executed(seeded, valid):
    payload = json.loads((Path(__file__).parents[1] / "examples/review-update.json").read_text())
    if not valid:
        payload["expected_hash"] = "bad"
    seeded.tables["inbox"].append(inbox_row(payload))
    before = seeded.read().digest
    result = audit_database(seeded)
    assert result["healthy"]
    assert result["inbox"]["unprocessed" if valid else "invalid"] == 1
    assert seeded.read().digest == before and seeded.commits == 1


@pytest.mark.parametrize("bad_payload", ['{"request_id":"x","request_id":"x"}', '{"x":NaN}'])
def test_duplicate_keys_and_nonfinite_inbox_warning(seeded, bad_payload):
    seeded.tables["inbox"].append(
        {"request_id": "x", "payload_json": bad_payload, "submitted_at": ""}
    )
    assert audit_database(seeded)["warning_count"] == 1


@pytest.mark.parametrize(
    "damage", ["hash", "receipt_date", "history_json", "history_request", "history_action"]
)
def test_operational_corruption_reports_location_without_raw_values(seeded, damage):
    if damage == "hash":
        seeded.tables["ingestion_runs"][0]["payload_hash"] = "private"
    elif damage == "receipt_date":
        seeded.tables["ingestion_runs"][0]["created_at"] = "private"
    elif damage == "history_json":
        seeded.tables["change_history"][0]["after_json"] = "private"
    elif damage == "history_request":
        seeded.tables["change_history"][0]["request_id"] = "private"
    else:
        seeded.tables["change_history"][0]["action"] = "private"
    original_read = seeded.read

    def physically_positioned():
        snapshot = original_read()
        snapshot.row_positions = {
            t: [3 + i * 2 for i in range(len(rows))] for t, rows in snapshot.tables.items()
        }
        return snapshot

    seeded.read = physically_positioned
    result = audit_database(seeded)
    assert not result["healthy"] and result["error_count"] == 1
    assert result["issues"][0]["row_number"] == 4
    assert "private" not in dumps(result)


def test_reference_corruption_is_sanitized(seeded):
    seeded.tables["restaurants"][0]["region_id"] = "private missing ID"
    result = audit_database(seeded)
    assert not result["healthy"]
    assert result["issues"][0]["code"] == "snapshot_invalid"
    assert "private" not in dumps(result)


def test_detail_limit_does_not_hide_failure(seeded):
    for row in seeded.tables["change_history"]:
        row["created_at"] = "invalid"
    result = audit_database(seeded, detail_limit=1)
    assert result["issue_count"] == 3 and len(result["issues"]) == 1
    assert result["issues_truncated"] and not result["healthy"]


@pytest.mark.parametrize("limit", [0, 1001])
def test_invalid_detail_limit_has_no_io(repository, limit):
    repository.read = lambda: pytest.fail("invalid limit must not read")
    with pytest.raises(ValueError):
        audit_database(repository, detail_limit=limit)


def test_invalid_physical_layout_is_reported(seeded):
    snapshot = seeded.read()
    snapshot.row_positions = {"regions": ["bad", 0]}
    seeded.read = lambda: snapshot
    result = audit_database(seeded)
    assert not result["healthy"] and result["issues"][0]["code"] == "physical_positions_invalid"


def test_audit_complete_e2e_and_restored_journals(repository):
    restored = type(repository)()
    repository.spreadsheet_id = "fictional-test"
    restored.spreadsheet_id = "fictional-restored"
    repository.metadata = restored.metadata = lambda: {
        "properties": {"title": "여행 DB 테스트 - audit"}
    }
    run_e2e(repository, restored, protected_id="master", request_id="audit-e2e", dry_run=False)
    for target in (repository, restored):
        result = audit_database(target)
        assert result["healthy"] and result["verification_queue"] == {
            "pending": 0,
            "resolved": 2,
            "pending_by_reason": {},
        }
    repository.tables["verification_queue"][0]["errors_json"] = "{}"
    assert not audit_database(repository)["healthy"]


def test_audit_cli_exit_codes_and_no_writes(monkeypatch, seeded, capsys):
    monkeypatch.setattr("travel_db.cli.SheetsRepository.from_env", lambda: seeded)
    assert main(["audit"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"]
    seeded.tables["change_history"][0]["after_json"] = "broken"
    assert main(["audit", "--detail-limit", "1"]) == 2
    assert not json.loads(capsys.readouterr().out)["healthy"]
    assert seeded.commits == 1


@pytest.mark.parametrize("count", [50, 100])
def test_offline_scale_scenario(count):
    from benchmark_scale import benchmark

    result = benchmark(count)
    assert result["checks_passed"] and result["unchanged"]


def test_unknown_queue_reason_does_not_echo_private_text(repository):
    from travel_db.live import sample_batch

    IngestionService(repository).ingest(sample_batch("queue-audit"), dry_run=False)
    repository.tables["verification_queue"][0]["reason"] = "private text"
    result = audit_database(repository)
    assert not result["healthy"] and "private text" not in dumps(result)
