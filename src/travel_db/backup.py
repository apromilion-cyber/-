"""Checksummed local backups and atomic restore into an explicitly selected empty Sheet."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter

from .models import Identifier
from .repository import ConflictError, DatabaseError, Snapshot, planned_snapshot
from .schema import HEADERS, SCHEMA_VERSION, dumps
from .service import stable_id, validate_snapshot


def checksum(value: dict) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def make_backup(snapshot: Snapshot, spreadsheet_id: str) -> dict:
    validate_snapshot(snapshot)
    body = {
        "backup_version": "1.0",
        "schema_version": SCHEMA_VERSION,
        "spreadsheet_id": spreadsheet_id,
        "digest": snapshot.digest,
        "tables": snapshot.tables,
        "row_positions": snapshot.row_positions,
        "sheet_ids": snapshot.sheet_ids,
    }
    return {**body, "checksum": checksum(body)}


def verify_backup(backup: dict) -> Snapshot:
    fields = {
        "backup_version",
        "schema_version",
        "spreadsheet_id",
        "digest",
        "tables",
        "row_positions",
        "sheet_ids",
        "checksum",
    }
    if not isinstance(backup, dict) or set(backup) != fields:
        raise ValueError("백업 형식이 잘못되었습니다. checksum을 포함한 새 백업을 사용하세요")
    if backup["backup_version"] != "1.0" or backup["schema_version"] != SCHEMA_VERSION:
        raise ValueError("지원하지 않는 백업/스키마 버전입니다")
    TypeAdapter(Identifier).validate_python(backup["spreadsheet_id"])
    body = {k: v for k, v in backup.items() if k != "checksum"}
    if checksum(body) != backup["checksum"]:
        raise ValueError("백업 checksum이 일치하지 않습니다")
    try:
        snapshot = Snapshot(backup["tables"], backup["row_positions"], backup["sheet_ids"])
        if (
            not isinstance(snapshot.tables, dict)
            or not isinstance(snapshot.row_positions, dict)
            or not isinstance(snapshot.sheet_ids, dict)
            or any(not isinstance(rows, list) for rows in snapshot.tables.values())
            or any(
                not isinstance(r, dict) or any(not isinstance(v, str) for v in r.values())
                for rows in snapshot.tables.values()
                for r in rows
            )
        ):
            raise ValueError("flat cell types")
        validate_snapshot(snapshot)
        if snapshot.row_positions:
            if set(snapshot.row_positions) != set(HEADERS):
                raise ValueError("layout tables")
            for table, positions in snapshot.row_positions.items():
                if (
                    len(positions) != len(snapshot.tables[table])
                    or len(set(positions)) != len(positions)
                    or any(type(p) is not int or p < 1 for p in positions)
                ):
                    raise ValueError("layout rows")
        if snapshot.sheet_ids and (
            set(snapshot.sheet_ids) != set(HEADERS)
            or len(set(snapshot.sheet_ids.values())) != len(HEADERS)
            or any(type(v) is not int or v < 0 for v in snapshot.sheet_ids.values())
        ):
            raise ValueError("layout sheet ids")
        if snapshot.digest != backup["digest"]:
            raise ValueError("snapshot digest")
    except (KeyError, TypeError, AttributeError, ValueError, DatabaseError) as exc:
        raise DatabaseError("백업의 행/참조/스냅샷 무결성이 손상되었습니다") from exc
    return snapshot


def save_backup(path: str, backup: dict):
    verify_backup(backup)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(backup, file, ensure_ascii=False, indent=2, allow_nan=False)


def load_backup(path: str, parser):
    if Path(path).stat().st_size > 50_000_000:
        raise ValueError("백업 파일은 50 MB 이하여야 합니다")
    return parser(Path(path).read_text(encoding="utf-8"))


def restore_backup(
    repository, backup: dict, *, request_id: str, protected_id: str, dry_run: bool = True
) -> dict:
    request_id = TypeAdapter(Identifier).validate_python(request_id)
    original = verify_backup(backup)
    if repository.spreadsheet_id in {backup["spreadsheet_id"], protected_id}:
        raise ConflictError(
            "백업 원본/마스터에는 전체 복구할 수 없습니다. 별도 빈 Sheets를 지정하세요"
        )
    snapshot = repository.read()
    validate_snapshot(snapshot)
    payload_hash = checksum(
        {
            "action": "backup_restore",
            "checksum": backup["checksum"],
            "target": repository.spreadsheet_id,
            "request_id": request_id,
        }
    )
    receipt = next((r for r in snapshot.tables["ingestion_runs"] if r["id"] == request_id), None)
    if receipt:
        if receipt["payload_hash"] != payload_hash:
            raise ConflictError("복구 request_id가 다른 요청과 충돌합니다")
        return {
            **json.loads(receipt["report_json"]),
            "dry_run": dry_run,
            "replayed": True,
            "committed": True,
        }
    if any(snapshot.tables.values()):
        raise ConflictError("복구 대상 관리 테이블은 모두 비어 있어야 합니다")
    now = datetime.now(timezone.utc).isoformat()
    report = {
        "request_id": request_id,
        "action": "backup_restore",
        "dry_run": dry_run,
        "committed": not dry_run,
        "replayed": False,
        "backup_checksum": backup["checksum"],
        "target_spreadsheet_id": repository.spreadsheet_id,
        "restored_rows": {t: len(rows) for t, rows in original.tables.items()},
    }
    additions = {t: list(rows) for t, rows in original.tables.items()}
    additions["change_history"].append(
        {
            "id": stable_id("hist", request_id + "/backup_restore"),
            "request_id": request_id,
            "entity_id": stable_id("backup", backup["checksum"]),
            "table": "database",
            "action": "backup_restore",
            "before_json": dumps(None),
            "after_json": dumps(report),
            "created_at": now,
        }
    )
    additions["ingestion_runs"].append(
        {
            "id": request_id,
            "payload_hash": payload_hash,
            "report_json": dumps(report),
            "created_at": now,
        }
    )
    validate_snapshot(planned_snapshot(snapshot, additions))
    if not dry_run:
        repository.commit(additions, snapshot)
    return report
