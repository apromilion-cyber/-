import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from travel_db.backup import checksum, make_backup, restore_backup, save_backup, verify_backup
from travel_db.cli import main, parse_json
from travel_db.live import run_e2e, sample_batch
from travel_db.repository import ConflictError, DatabaseError
from travel_db.research import ResearchRequest, plan_research
from travel_db.schema import dumps
from travel_db.service import IngestionService


@pytest.fixture
def backed(repository, sample):
    repository.spreadsheet_id = "fictional-source"
    IngestionService(repository).ingest(sample, dry_run=False)
    return make_backup(repository.read(), repository.spreadsheet_id)


def destination(repository):
    target = type(repository)()
    target.spreadsheet_id = "fictional-restore"
    return target


def reseal(value):
    value["checksum"] = checksum({k: v for k, v in value.items() if k != "checksum"})


def test_backup_checksum_and_snapshot_roundtrip(repository, backed):
    copied = parse_json(dumps(backed))
    assert verify_backup(copied).digest == repository.read().digest
    assert copied["tables"] == repository.tables


def test_backup_local_permissions_and_existing_file_preservation(tmp_path, backed):
    path = tmp_path / "backup.json"
    save_backup(str(path), backed)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert verify_backup(parse_json(path.read_text())).tables == backed["tables"]
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        save_backup(str(path), backed)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["tables", "spreadsheet_id", "digest", "row_positions", "sheet_ids"]
)
def test_backup_tampering_changes_checksum(backed, field):
    backed[field] = "tampered"
    with pytest.raises(ValueError, match="checksum"):
        verify_backup(backed)


@pytest.mark.parametrize(
    "damage", ["reference", "currency", "duplicate", "cell_type", "layout", "digest"]
)
def test_recomputed_checksum_still_requires_valid_data(backed, damage):
    if damage == "reference":
        backed["tables"]["restaurants"][0]["region_id"] = "missing"
    elif damage == "currency":
        backed["tables"]["restaurants"][0]["price"] = (
            '{"amount":"1","currency":"INVALID","basis":"test"}'
        )
    elif damage == "duplicate":
        backed["tables"]["restaurants"].append(deepcopy(backed["tables"]["restaurants"][0]))
    elif damage == "cell_type":
        backed["tables"]["change_history"][0]["before_json"] = 7
    elif damage == "layout":
        backed["row_positions"] = {"regions": [0]}
    else:
        backed["digest"] = "invalid"
    reseal(backed)
    with pytest.raises(DatabaseError, match="손상"):
        verify_backup(backed)


def test_backup_format_mismatch_is_explicit(backed):
    backed.pop("checksum")
    with pytest.raises(ValueError, match="새 백업"):
        verify_backup(backed)


def test_backup_restore_dry_run_atomic_apply_and_replay(repository, backed):
    target = destination(repository)
    snapshot = target.read().digest
    dry = restore_backup(target, backed, request_id="restore-001", protected_id="master")
    assert dry["dry_run"] and not dry["committed"]
    assert target.read().digest == snapshot
    result = restore_backup(
        target, backed, request_id="restore-001", protected_id="master", dry_run=False
    )
    assert result["committed"] and target.commits == 1
    assert target.tables["restaurants"] == backed["tables"]["restaurants"]
    assert target.tables["change_history"][-1]["action"] == "backup_restore"
    assert len(target.tables["ingestion_runs"]) == len(backed["tables"]["ingestion_runs"]) + 1
    applied = target.read().digest
    assert restore_backup(
        target, backed, request_id="restore-001", protected_id="master", dry_run=False
    )["replayed"]
    assert target.read().digest == applied and target.commits == 1


@pytest.mark.parametrize("target_id", ["fictional-source", "master"])
def test_backup_restore_refuses_source_or_master_before_write(repository, backed, target_id):
    target = destination(repository)
    target.spreadsheet_id = target_id
    with pytest.raises(ConflictError, match="원본/마스터"):
        restore_backup(
            target, backed, request_id="restore-001", protected_id="master", dry_run=False
        )
    assert target.commits == 0


def test_backup_restore_refuses_populated_destination(repository, backed):
    target = destination(repository)
    target.tables["inbox"].append(
        {"request_id": "existing", "payload_json": "{}", "submitted_at": ""}
    )
    before = target.read().digest
    with pytest.raises(ConflictError, match="비어"):
        restore_backup(
            target, backed, request_id="restore-001", protected_id="master", dry_run=False
        )
    assert target.read().digest == before


def test_backup_restore_request_collision_and_atomic_failure(repository, backed):
    target = destination(repository)
    target.fail_commit = True
    before = target.read().digest
    with pytest.raises(DatabaseError):
        restore_backup(
            target, backed, request_id="restore-001", protected_id="master", dry_run=False
        )
    assert target.read().digest == before
    target.fail_commit = False
    with pytest.raises(DatabaseError):
        restore_backup(
            target, backed, request_id="sample-country", protected_id="master", dry_run=False
        )
    assert target.read().digest == before


def test_verify_backup_cli_without_credentials(monkeypatch, tmp_path, backed, capsys):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    path = tmp_path / "backup.json"
    path.write_text(dumps(backed))
    assert main(["verify-backup", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]


def test_research_plan_registered_scope_and_existing_records(repository, backed):
    before = repository.read().digest
    payload = {
        "schema_version": "1.0",
        "request_id": "explicit-user-research",
        "category": "음식점",
        "country": "JP",
        "city": "샘플 도시 (가상)",
        "count": 50,
    }
    plan = plan_research(repository, payload)
    assert plan["read_only"] and plan["requested_count"] == 50
    assert plan["scope"]["region_id"] == "sample-city"
    assert plan["existing_count"] == 1
    assert plan["classification_hints"] == {"kind": "restaurant", "region_id": "sample-city"}
    assert repository.read().digest == before


@pytest.mark.parametrize(
    "damage", ["unknown_region", "unknown_category", "zero_count", "extra", "no_scope"]
)
def test_research_plan_does_not_invent_scope_or_collect(repository, backed, damage):
    payload = {
        "schema_version": "1.0",
        "request_id": "explicit-user-research",
        "category": "음식점",
        "region_id": "sample-city",
        "count": 50,
    }
    if damage == "unknown_region":
        payload["region_id"] = "unknown"
    elif damage == "unknown_category":
        payload["category"] = "unknown-category"
    elif damage == "zero_count":
        payload["count"] = 0
    elif damage == "extra":
        payload["automatic"] = True
    else:
        payload.pop("region_id")
    before = repository.read().digest
    with pytest.raises(ValueError):
        plan_research(repository, payload)
    assert repository.read().digest == before


def test_research_plan_subtype_excludes_other_experiences(repository, sample):
    IngestionService(repository).ingest(sample, dry_run=False)
    base = deepcopy(sample["items"][-1])
    base.update(kind="experience", experience_type="cafe", id="cafe")
    nature = {**deepcopy(base), "id": "nature", "name": "가상 자연", "experience_type": "nature"}
    IngestionService(repository).ingest(
        {"schema_version": "1.0", "request_id": "experiences", "items": [base, nature]},
        dry_run=False,
    )
    plan = plan_research(
        repository,
        {
            "schema_version": "1.0",
            "request_id": "research-cafes",
            "region_id": "sample-city",
            "category": "카페",
            "count": 2,
        },
    )
    assert plan["existing_count"] == 1 and plan["existing_items"][0]["id"] == "cafe"


def test_research_schema_matches_document():
    path = Path(__file__).parents[1] / "docs/research.schema.json"
    assert json.loads(path.read_text()) == ResearchRequest.model_json_schema()


def test_e2e_dry_run_and_roundtrip_only_fictional_data(repository):
    target = destination(repository)
    repository.spreadsheet_id = "fictional-e2e"
    for repo in (repository, target):
        repo.metadata = lambda: {"properties": {"title": "여행 DB 테스트 - 가상 파일"}}
    before = repository.read().digest, target.read().digest
    dry = run_e2e(repository, target, protected_id="master", request_id="e2e-sample")
    assert not dry["committed"] and dry["passed"] is None
    assert (repository.read().digest, target.read().digest) == before
    result = run_e2e(
        repository, target, protected_id="master", request_id="e2e-sample", dry_run=False
    )
    assert result["passed"] and len(result["checks"]) == 8
    assert len(repository.tables["restaurants"]) == len(target.tables["restaurants"]) == 1
    first = repository.read().digest, target.read().digest
    replay = run_e2e(
        repository, target, protected_id="master", request_id="e2e-sample", dry_run=False
    )
    assert replay["passed"] and replay["restore_report"]["replayed"]
    assert (repository.read().digest, target.read().digest) == first


def test_e2e_refuses_master_and_non_test_title(repository):
    target = destination(repository)
    repository.spreadsheet_id = "master"
    with pytest.raises(ConflictError, match="서로 다른"):
        run_e2e(repository, target, protected_id="master", request_id="live")
    repository.spreadsheet_id = "test"
    repository.metadata = lambda: {"properties": {"title": "일반 여행 DB"}}
    with pytest.raises(ConflictError, match="제목"):
        run_e2e(repository, target, protected_id="master", request_id="live")
    assert repository.commits == target.commits == 0


def test_e2e_refuses_unrelated_existing_data(repository, backed):
    target = destination(repository)
    for repo in (repository, target):
        repo.metadata = lambda: {"properties": {"title": "여행 DB 테스트 - 가상 파일"}}
    with pytest.raises(ConflictError, match="빈 파일"):
        run_e2e(repository, target, protected_id="master", request_id="different-run")


def test_fictional_live_sample_identity_and_quarantine(repository):
    sample = sample_batch("live-fake-001")
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert (report["created"], report["queued"]) == (3, 2)
    assert all("example.com" in r["url"] for r in repository.tables["sources"])
    assert {q["reason"] for q in repository.tables["verification_queue"]} == {
        "change_candidate",
        "validation_error",
    }


def test_search_pagination_digest_and_pending_cli(monkeypatch, repository, backed, capsys):
    monkeypatch.setattr("travel_db.cli.SheetsRepository.from_env", lambda: repository)
    assert main(["search", "--table", "regions", "--limit", "1"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["next_offset"] == 1
    assert (
        main(
            [
                "search",
                "--table",
                "regions",
                "--limit",
                "1",
                "--offset",
                "1",
                "--expected-digest",
                first["digest"],
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["next_offset"] is None and second["rows"] != first["rows"]
    assert main(["search", "--table", "regions", "--expected-digest", "stale"]) == 2
    capsys.readouterr()
    assert main(["pending"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 0


def test_backup_export_cli_checksum(monkeypatch, repository, backed, tmp_path, capsys):
    monkeypatch.setattr("travel_db.cli.SheetsRepository.from_env", lambda: repository)
    path = tmp_path / "export.json"
    assert main(["backup", "--output", str(path)]) == 0
    emitted = json.loads(capsys.readouterr().out)
    data = json.loads(path.read_text())
    assert emitted["checksum"] == data["checksum"]
    assert verify_backup(data).tables == repository.tables


@pytest.mark.parametrize("dry_run", [True, False])
def test_oversized_restore_fails_before_any_partial_batch(repository, dry_run):
    repository.spreadsheet_id = "fictional-source"
    for index in range(110):
        repository.tables["inbox"].append(
            {
                "request_id": f"sample-large-{index}",
                "payload_json": "{}" + " " * 20_000,
                "submitted_at": "",
            }
        )
    backup = make_backup(repository.read(), repository.spreadsheet_id)
    target = destination(repository)
    before = target.read().digest
    with pytest.raises(DatabaseError, match="2 MB"):
        restore_backup(
            target, backup, request_id="restore-large", protected_id="master", dry_run=dry_run
        )
    assert target.read().digest == before and target.commits == 0


def test_research_planning_includes_neighborhoods_without_unrelated_regions(repository, sample):
    neighborhood = deepcopy(sample["items"][1])
    neighborhood.update(
        id="sample-neighborhood",
        region_id="sample-city",
        parent_region_id="sample-city",
        level="neighborhood",
        name="가상 세부 지역",
    )
    restaurant = deepcopy(sample["items"][-1])
    restaurant.update(
        id="neighborhood-restaurant", region_id="sample-neighborhood", name="다른 가상 점포"
    )
    sample["items"].extend([neighborhood, restaurant])
    IngestionService(repository).ingest(sample, dry_run=False)
    plan = plan_research(
        repository,
        {
            "schema_version": "1.0",
            "request_id": "explicit-scope",
            "region_id": "sample-city",
            "category": "음식점",
            "count": 50,
        },
    )
    assert plan["existing_count"] == 2
    assert {r["region_id"] for r in plan["existing_items"]} == {
        "sample-city",
        "sample-neighborhood",
    }


def test_e2e_reuses_identical_backup_without_overwriting(repository, tmp_path):
    target = destination(repository)
    repository.spreadsheet_id = "fictional-e2e"
    for repo in (repository, target):
        repo.metadata = lambda: {"properties": {"title": "여행 DB 테스트 - 샘플"}}
    path = tmp_path / "backup.json"
    run_e2e(
        repository,
        target,
        protected_id="master",
        request_id="e2e-file",
        dry_run=False,
        backup_output=str(path),
    )
    before = path.read_bytes(), repository.commits, target.commits
    assert run_e2e(
        repository,
        target,
        protected_id="master",
        request_id="e2e-file",
        dry_run=False,
        backup_output=str(path),
    )["passed"]
    assert (path.read_bytes(), repository.commits, target.commits) == before


def test_research_page_bounds_and_digest_conflict(repository, backed):
    payload = {
        "schema_version": "1.0",
        "request_id": "explicit-plan",
        "region_id": "sample-country",
        "category": "지역",
        "count": 2,
        "existing_limit": 1,
    }
    first = plan_research(repository, payload)
    assert first["existing_count"] == 2 and first["next_offset"] == 1
    second = plan_research(
        repository, {**payload, "offset": 1, "expected_digest": first["snapshot_digest"]}
    )
    assert second["next_offset"] is None and first["existing_items"] != second["existing_items"]
    with pytest.raises(ConflictError, match="페이지 사이"):
        plan_research(repository, {**payload, "expected_digest": "0" * 64})


def test_research_plan_cli_is_read_only(monkeypatch, repository, backed, tmp_path, capsys):
    monkeypatch.setattr("travel_db.cli.SheetsRepository.from_env", lambda: repository)
    path = tmp_path / "plan.json"
    path.write_text(
        dumps(
            {
                "schema_version": "1.0",
                "request_id": "explicit-plan",
                "region_id": "sample-city",
                "category": "음식점",
                "count": 50,
            }
        )
    )
    before = repository.read().digest
    assert main(["plan-research", str(path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["read_only"] and plan["existing_items"][0]["address"] == "가상 주소 1"
    assert repository.read().digest == before
