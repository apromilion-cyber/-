"""Explicit user-triggered commands; dry-run is the default for every database write."""

import argparse
import fcntl
import hashlib
import json
import os
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pydantic import ValidationError

from .audit import audit_database
from .backup import load_backup, make_backup, restore_backup, save_backup, verify_backup
from .classification import ClassificationError, prepare_item
from .jsonio import parse_json
from .live import run_e2e
from .models import ENTITY_ADAPTER, INPUT_ADAPTER, Batch
from .operations import OPERATION_ADAPTER
from .repository import DatabaseError, SheetsRepository
from .research import ResearchRequest, plan_research
from .review import ReviewService, inspect_entity, pending_reviews
from .schema import HEADERS
from .service import IngestionService, normalize, validate_snapshot, validation_errors


def load_json(path: str):
    if Path(path).stat().st_size > 1_000_000:
        raise ValueError("JSON 입력 파일은 1 MB 이하여야 합니다")
    return parse_json(Path(path).read_text(encoding="utf-8"))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


@contextmanager
def writer_lock(spreadsheet_id: str):
    """Only serializes writers on this machine; Sheets has no global transaction lock."""
    digest = hashlib.sha256(spreadsheet_id.encode()).hexdigest()
    path = f"/tmp/travel-db-{digest}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DatabaseError("이 Codespace에서 다른 쓰기 작업이 실행 중입니다") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def validate_input(payload: dict) -> dict:
    batch = Batch.model_validate(payload)
    results = []
    for index, item in enumerate(batch.items):
        try:
            if batch.schema_version == "1.1":
                prepared, rules = prepare_item(item)
                results.append(
                    {
                        "item_index": index,
                        "valid": True,
                        "kind": prepared["kind"],
                        "classification_rules": rules,
                        "validation_mode": "input_only",
                    }
                )
                continue
            entity = ENTITY_ADAPTER.validate_python(item)
            results.append(
                {
                    "item_index": index,
                    "valid": True,
                    "verification_status": entity.verification_status,
                }
            )
        except ValidationError as exc:
            results.append({"item_index": index, "valid": False, "errors": validation_errors(exc)})
        except ClassificationError as exc:
            results.append({"item_index": index, "valid": False, "errors": [exc.detail()]})
    return {
        "request_id": batch.request_id,
        "valid": all(r["valid"] for r in results),
        "results": results,
        "reference_checks": "ingest에서 원본 DB와 함께 지역 분류·최종 모델·참조 검사",
    }


def process_inbox(repository, *, dry_run: bool = True) -> dict:
    snapshot = repository.read()
    validate_snapshot(snapshot)
    service = IngestionService(repository)
    reports = []
    for row in snapshot.tables["inbox"]:
        try:
            payload = parse_json(row["payload_json"])
            if not isinstance(payload, dict) or payload.get("request_id") != row["request_id"]:
                raise ValueError("inbox와 JSON의 request_id가 다릅니다")
            if "action" in payload:
                reports.append(ReviewService(repository).execute(payload, dry_run=dry_run))
            else:
                reports.append(service.ingest(payload, dry_run=dry_run))
        except ValidationError as exc:
            reports.append({"request_id": row["request_id"], "error": validation_errors(exc)})
        except (ValueError, DatabaseError) as exc:
            reports.append({"request_id": row["request_id"], "error": str(exc)})
            # Abort on uncertain commit/DB conflicts; do not start another write.
            if isinstance(exc, DatabaseError):
                break
    return {"dry_run": dry_run, "requests": reports}


def parser():
    result = argparse.ArgumentParser(description="개인 여행 Google Sheets 데이터베이스")
    commands = result.add_subparsers(dest="command", required=True)
    schema = commands.add_parser("schema", help="입력 JSON Schema 생성 (인증 불필요)")
    schema.add_argument("--output")
    validate = commands.add_parser("validate", help="입력 형식 검사 (인증 불필요)")
    validate.add_argument("input")
    operation_schema = commands.add_parser("operation-schema", help="승인/수정/복구 JSON Schema")
    operation_schema.add_argument("--output")
    validate_operation = commands.add_parser("validate-operation", help="작업 JSON 형식 검사")
    validate_operation.add_argument("input")
    research_schema = commands.add_parser("research-schema", help="사용자 조사 요청 JSON Schema")
    research_schema.add_argument("--output")
    research = commands.add_parser("plan-research", help="명시적 조사 요청의 범위/기존 항목 조회")
    research.add_argument("input")
    page = commands.add_parser("page", help="한 탭의 유한 물리 행 페이지 조회")
    page.add_argument("--table", required=True, choices=list(HEADERS))
    page.add_argument("--start-row", type=int, default=2)
    page.add_argument("--page-size", type=int, default=200)
    operate = commands.add_parser("operate", help="승인·거부·업데이트·이전값 복구")
    operate.add_argument("input")
    operate.add_argument("--apply", action="store_true")
    inspect = commands.add_parser("inspect", help="대상 값·필드 출처·충돌 검사 해시 조회")
    inspect.add_argument("id")
    pending = commands.add_parser("pending", help="미처리 검증 대기 조회")
    pending.add_argument("--limit", type=int, default=50)
    audit = commands.add_parser("audit", help="읽기 전용 DB 무결성·이력·입력함 운영 점검")
    audit.add_argument("--detail-limit", type=int, default=50)
    init = commands.add_parser("init", help="누락된 DB 탭 초기화")
    init.add_argument("--apply", action="store_true")
    ingest = commands.add_parser("ingest", help="검증 후 신규 추가 또는 대기 분리")
    ingest.add_argument("input")
    ingest.add_argument("--apply", action="store_true")
    inbox = commands.add_parser("process-inbox", help="Drive 입력함을 사용자 명령으로 처리")
    inbox.add_argument("--apply", action="store_true")
    search = commands.add_parser("search", help="기존 행 검색")
    search.add_argument("--table", choices=list(HEADERS), required=True)
    search.add_argument("--query", default="")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--expected-digest")
    backup = commands.add_parser("backup", help="명시적 로컬 백업 (운영 DB 아님)")
    backup.add_argument("--output", required=True)
    verify = commands.add_parser("verify-backup", help="인증 없이 백업 checksum/참조 검증")
    verify.add_argument("input")
    restore = commands.add_parser("restore-backup", help="별도 빈 Sheets로 백업 전체 복구")
    restore.add_argument("input")
    restore.add_argument("--target-spreadsheet-id", required=True)
    restore.add_argument("--request-id", required=True)
    restore.add_argument("--apply", action="store_true")
    e2e = commands.add_parser("e2e", help="명시적 별도 테스트 시트의 샘플 E2E")
    e2e.add_argument("--test-spreadsheet-id", required=True)
    e2e.add_argument("--restore-spreadsheet-id", required=True)
    e2e.add_argument("--request-id", required=True)
    e2e.add_argument("--backup-output")
    e2e.add_argument("--apply", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"schema", "operation-schema", "research-schema"}:
            adapter = {
                "schema": INPUT_ADAPTER,
                "operation-schema": OPERATION_ADAPTER,
                "research-schema": ResearchRequest,
            }[args.command]
            if args.command == "research-schema":
                schema = adapter.model_json_schema()
            else:
                schema = adapter.json_schema()
            if args.output:
                Path(args.output).write_text(
                    json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                emit({"output": args.output})
            else:
                emit(schema)
            return 0
        if args.command == "validate":
            result = validate_input(load_json(args.input))
            emit(result)
            return 0 if result["valid"] else 2
        if args.command == "validate-operation":
            operation = OPERATION_ADAPTER.validate_python(load_json(args.input))
            emit(
                {
                    "request_id": operation.request_id,
                    "action": operation.action,
                    "valid": True,
                    "validation_mode": "input_only",
                    "database_checks": "operate dry-run에서 대상·충돌·출처·참조 검사",
                }
            )
            return 0
        if args.command == "verify-backup":
            backup = load_backup(args.input, parse_json)
            snapshot = verify_backup(backup)
            emit(
                {
                    "valid": True,
                    "checksum": backup["checksum"],
                    "row_counts": {t: len(rows) for t, rows in snapshot.tables.items()},
                }
            )
            return 0
        repository = SheetsRepository.from_env()
        if args.command == "audit":
            result = audit_database(repository, detail_limit=args.detail_limit)
            emit(result)
            return 0 if result["healthy"] else 2
        if args.command == "plan-research":
            emit(plan_research(repository, load_json(args.input)))
            return 0
        if args.command == "page":
            emit(
                repository.read_page(args.table, start_row=args.start_row, page_size=args.page_size)
            )
            return 0
        if args.command == "restore-backup":
            target = SheetsRepository(args.target_spreadsheet_id, repository.session)
            with writer_lock(target.spreadsheet_id):
                result = restore_backup(
                    target,
                    load_backup(args.input, parse_json),
                    request_id=args.request_id,
                    protected_id=repository.spreadsheet_id,
                    dry_run=not args.apply,
                )
            emit(result)
            return 0
        if args.command == "e2e":
            test = SheetsRepository(args.test_spreadsheet_id, repository.session)
            restored = SheetsRepository(args.restore_spreadsheet_id, repository.session)
            with ExitStack() as stack:
                for spreadsheet_id in sorted({test.spreadsheet_id, restored.spreadsheet_id}):
                    stack.enter_context(writer_lock(spreadsheet_id))
                result = run_e2e(
                    test,
                    restored,
                    protected_id=repository.spreadsheet_id,
                    request_id=args.request_id,
                    dry_run=not args.apply,
                    backup_output=args.backup_output,
                    progress=lambda item: print(
                        json.dumps(item, ensure_ascii=False), file=sys.stderr, flush=True
                    ),
                )
            emit(result)
            return 0
        if args.command in {"init", "ingest", "process-inbox", "operate"}:
            with writer_lock(repository.spreadsheet_id):
                if args.command == "init":
                    result = repository.initialize(dry_run=not args.apply)
                elif args.command == "ingest":
                    result = IngestionService(repository).ingest(
                        load_json(args.input), dry_run=not args.apply
                    )
                elif args.command == "operate":
                    result = ReviewService(repository).execute(
                        load_json(args.input), dry_run=not args.apply
                    )
                else:
                    result = process_inbox(repository, dry_run=not args.apply)
            emit(result)
            return 2 if any("error" in r for r in result.get("requests", [])) else 0
        if args.command == "inspect":
            emit(inspect_entity(repository, args.id))
            return 0
        if args.command == "pending":
            if not 1 <= args.limit <= 1000:
                raise ValueError("limit은 1~1000이어야 합니다")
            result = pending_reviews(repository)
            result["rows"] = result["rows"][: args.limit]
            emit(result)
            return 0
        snapshot = repository.read()
        validate_snapshot(snapshot)
        if args.command == "search":
            if not 1 <= args.limit <= 1000:
                raise ValueError("limit은 1~1000이어야 합니다")
            if args.offset < 0:
                raise ValueError("offset은 0 이상이어야 합니다")
            if args.expected_digest and args.expected_digest != snapshot.digest:
                raise DatabaseError(
                    "검색 페이지 사이 DB가 변경되었습니다. 처음부터 다시 조회하세요"
                )
            query = normalize(args.query)
            matches = [
                r
                for r in snapshot.tables[args.table]
                if any(query in normalize(v) for v in r.values())
            ]
            emit(
                {
                    "table": args.table,
                    "total_matches": len(matches),
                    "digest": snapshot.digest,
                    "next_offset": args.offset + args.limit
                    if args.offset + args.limit < len(matches)
                    else None,
                    "rows": matches[args.offset : args.offset + args.limit],
                }
            )
        elif args.command == "backup":
            backup = make_backup(snapshot, repository.spreadsheet_id)
            save_backup(args.output, backup)
            emit({"output": args.output, "digest": snapshot.digest, "checksum": backup["checksum"]})
        return 0
    except ValidationError as exc:
        emit({"error": "요청 형식 검증 실패", "details": validation_errors(exc)})
        return 2
    except (DatabaseError, ValueError, OSError) as exc:
        emit({"error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
