"""Opt-in fictional-data E2E on two explicitly named, isolated test spreadsheets."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from pydantic import TypeAdapter

from .backup import load_backup, make_backup, restore_backup, save_backup, verify_backup
from .jsonio import parse_json
from .models import Identifier
from .provenance import entity_hash
from .repository import ConflictError, DatabaseError
from .review import ReviewService, inspect_entity, pending_reviews
from .service import IngestionService, validate_snapshot

TEST_TITLE_PREFIX = "여행 DB 테스트 -"


def sample_batch(request_id):
    suffix = hashlib.sha256(request_id.encode()).hexdigest()[:12]
    common = {
        "local_name": None,
        "researched_on": "2026-01-01",
        "last_verified_on": "2026-01-01",
        "verification_status": "verified",
    }
    country_id, city_id, restaurant_id = (
        f"e2e-{k}-{suffix}" for k in ("country", "city", "restaurant")
    )

    def sources(kind):
        return [
            {
                "url": f"https://example.com/travel-db-e2e/{suffix}/{kind}",
                "title": "실제 정보가 아닌 테스트 샘플",
                "accessed_on": "2026-01-01",
            }
        ]

    country = {
        **common,
        "id": country_id,
        "kind": "region",
        "region_id": None,
        "parent_region_id": None,
        "name": "E2E 가상 국가",
        "level": "country",
        "country_code": "JP",
        "sources": sources("country"),
    }
    city = {
        **common,
        "id": city_id,
        "kind": "region",
        "region_id": country_id,
        "parent_region_id": country_id,
        "name": "E2E 가상 도시",
        "level": "city",
        "country_code": "JP",
        "sources": sources("city"),
    }
    restaurant = {
        **common,
        "id": restaurant_id,
        "kind": "restaurant",
        "region_id": city_id,
        "name": "E2E 가상 식당",
        "address": "가상 주소 1",
        "sources": sources("restaurant"),
        "price": {"amount": "1000", "currency": "JPY", "basis": "가상 테스트 1식"},
    }
    changed = deepcopy(restaurant)
    changed["price"]["amount"] = "1200"
    invalid = deepcopy(restaurant)
    invalid["id"] = "e2e-invalid-" + suffix
    invalid["price"]["currency"] = "INVALID"
    return {
        "schema_version": "1.0",
        "request_id": request_id + "-seed",
        "items": [country, city, restaurant, changed, invalid],
    }


def run_e2e(
    test_repository,
    restore_repository,
    *,
    protected_id: str,
    request_id: str,
    dry_run: bool = True,
    backup_output: str | None = None,
    progress=None,
):
    TypeAdapter(Identifier).validate_python(request_id)
    if len(request_id) > 100:
        raise ValueError("E2E request_id는 접미사를 위해 100자 이하여야 합니다")
    ids = {test_repository.spreadsheet_id, restore_repository.spreadsheet_id, protected_id}
    if len(ids) != 3:
        raise ConflictError("E2E·복구 대상·마스터는 서로 다른 파일이어야 합니다")
    for repository in (test_repository, restore_repository):
        if (
            not repository.metadata()
            .get("properties", {})
            .get("title", "")
            .startswith(TEST_TITLE_PREFIX)
        ):
            raise ConflictError(f"테스트 파일 제목은 '{TEST_TITLE_PREFIX}'로 시작해야 합니다")
    initial = test_repository.read()
    restored_initial = restore_repository.read()
    validate_snapshot(initial)
    validate_snapshot(restored_initial)
    seed = sample_batch(request_id)
    if any(initial.tables.values()) and not any(
        r["id"] == seed["request_id"] for r in initial.tables["ingestion_runs"]
    ):
        raise ConflictError("E2E 대상은 빈 파일 또는 동일 request_id의 테스트 기록이어야 합니다")
    restore_request = request_id + "-backup-restore"
    if any(restored_initial.tables.values()) and not any(
        r["id"] == restore_request for r in restored_initial.tables["ingestion_runs"]
    ):
        raise ConflictError("복구 테스트 대상은 빈 파일이어야 합니다")
    ingestion = IngestionService(test_repository)
    seed_dry = ingestion.ingest(seed)
    planned = {
        "request_id": request_id,
        "dry_run": dry_run,
        "test_spreadsheet_id": test_repository.spreadsheet_id,
        "restore_spreadsheet_id": restore_repository.spreadsheet_id,
        "seed_plan": seed_dry,
        "planned_steps": [
            "신규/대기 저장 및 재조회",
            "동일 요청 재실행",
            "승인 업데이트",
            "거부",
            "이전값 복구",
            "백업 검증 및 별도 파일 복원",
        ],
    }
    if dry_run:
        return {**planned, "passed": None, "committed": False}
    steps = []

    def check(name, condition):
        if not condition:
            raise DatabaseError(f"E2E 검증 실패: {name} (테스트 파일만 사용)")
        steps.append(name)
        if progress:
            progress({"step": name, "passed": True})

    seed_report = ingestion.ingest(seed, dry_run=False)
    after_seed = test_repository.read()
    entities, _ = validate_snapshot(after_seed)
    expected_ids = {r["id"] for r in seed["items"][:3]}
    check(
        "신규 3개/대기 2개 및 재조회",
        set(entities) == expected_ids
        and seed_report["created"] == 3
        and seed_report["queued"] == 2,
    )
    check("동일 요청 멱등성", ingestion.ingest(seed, dry_run=False)["replayed"])
    restaurant = seed["items"][2]
    create_history = next(
        r
        for r in after_seed.tables["change_history"]
        if r["entity_id"] == restaurant["id"] and r["action"] == "create"
    )
    seed_row = json.loads(create_history["after_json"])
    queue_change = next(
        r for r in after_seed.tables["verification_queue"] if r["reason"] == "change_candidate"
    )
    queue_invalid = next(
        r for r in after_seed.tables["verification_queue"] if r["reason"] == "validation_error"
    )
    common = {
        "schema_version": "1.0",
        "reviewer": "E2E 가상 검토자",
        "reason": "사용자가 지시한 가상 라이브 테스트",
    }
    approval = {
        **common,
        "action": "approve",
        "request_id": request_id + "-approve",
        "queue_id": queue_change["id"],
        "mode": "update",
        "target_id": restaurant["id"],
        "expected_hash": entity_hash(seed_row, {}),
        "entity": seed["items"][3],
        "field_sources": {"price": [restaurant["sources"][0]["url"]]},
    }
    reviews = ReviewService(test_repository)
    approved = reviews.execute(approval, dry_run=False)
    approval_replayed = approved["replayed"]
    if not approval_replayed:
        checked = inspect_entity(test_repository, restaurant["id"])
        check(
            "승인 수정/필드 근거 재조회",
            json.loads(checked["entity"]["price"])["amount"] == "1200"
            and "price" in checked["field_sources"],
        )
    else:
        check("승인 영수증 재실행", True)
    rejection = {
        **common,
        "action": "reject",
        "request_id": request_id + "-reject",
        "queue_id": queue_invalid["id"],
    }
    reviews.execute(rejection, dry_run=False)
    check("거부 후 대기 분리 유지", pending_reviews(test_repository)["pending"] == 0)
    restoration = {
        **common,
        "action": "restore",
        "request_id": request_id + "-restore",
        "history_id": approved["history_id"],
        "expected_hash": approved["expected_hash_after"],
    }
    reviews.execute(restoration, dry_run=False)
    final_inspection = inspect_entity(test_repository, restaurant["id"])
    check(
        "이전값과 근거 복구 재조회",
        json.loads(final_inspection["entity"]["price"])["amount"] == "1000"
        and final_inspection["field_sources"] == {},
    )
    source_snapshot = test_repository.read()
    backup = make_backup(source_snapshot, test_repository.spreadsheet_id)
    verify_backup(backup)
    if backup_output:
        if Path(backup_output).exists():
            existing = load_backup(backup_output, parse_json)
            verify_backup(existing)
            if existing["checksum"] != backup["checksum"]:
                raise ConflictError("동일 경로에 다른 백업이 있습니다. 덮어쓰지 않습니다")
        else:
            save_backup(backup_output, backup)
    restore_dry = restore_backup(
        restore_repository, backup, request_id=restore_request, protected_id=protected_id
    )
    restore_report = restore_backup(
        restore_repository,
        backup,
        request_id=restore_request,
        protected_id=protected_id,
        dry_run=False,
    )
    restored = restore_repository.read()
    validate_snapshot(restored)
    check(
        "백업 checksum/별도 파일 원자 복원",
        all(
            restored.tables[t][: len(source_snapshot.tables[t])] == source_snapshot.tables[t]
            for t in source_snapshot.tables
        ),
    )
    check(
        "복원 데이터/대기/감사 기록 재조회",
        len(restored.tables["restaurants"]) == 1
        and len(restored.tables["review_decisions"]) == 2
        and len(restored.tables["ingestion_runs"])
        == len(source_snapshot.tables["ingestion_runs"]) + 1,
    )
    check(
        "복원 재실행 멱등성",
        restore_backup(
            restore_repository,
            backup,
            request_id=restore_request,
            protected_id=protected_id,
            dry_run=False,
        )["replayed"],
    )
    return {
        **planned,
        "passed": True,
        "committed": True,
        "checks": steps,
        "backup_checksum": backup["checksum"],
        "backup_output": backup_output,
        "restore_report": restore_report,
        "restore_dry_run": restore_dry["dry_run"],
        "source_row_counts": {t: len(rows) for t, rows in source_snapshot.tables.items()},
        "restore_row_counts": {t: len(rows) for t, rows in restored.tables.items()},
    }
