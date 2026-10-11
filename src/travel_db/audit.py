"""Read-only operational checks. Never repair rows or execute inbox requests."""

import hashlib
import re
from collections import Counter

from pydantic import ValidationError

from .jsonio import parse_json
from .models import Batch
from .operations import OPERATION_ADAPTER
from .provenance import timestamp
from .repository import DatabaseError
from .schema import HEADERS, dumps
from .service import validate_snapshot

QUEUE_REASONS = {
    "classification_error",
    "validation_error",
    "needs_verification",
    "id_conflict",
    "duplicate_candidate",
    "change_candidate",
    "reference_error",
}


def audit_database(repository, *, detail_limit: int = 50) -> dict:
    if not 1 <= detail_limit <= 1000:
        raise ValueError("detail_limit은 1~1000이어야 합니다")
    snapshot = repository.read()
    issues = []

    def issue(table, index, code, severity="error"):
        positions = snapshot.row_positions.get(table, [])
        issues.append(
            {
                "table": table,
                "row_number": positions[index] + 1
                if index < len(positions) and type(positions[index]) is int
                else None,
                "record_index": index,
                "code": code,
                "severity": severity,
            }
        )

    report = {
        "read_only": True,
        "validation_mode": "full_snapshot_and_journals",
        "snapshot_digest": snapshot.digest,
        "row_counts": {table: len(rows) for table, rows in snapshot.tables.items()},
    }
    try:
        validate_snapshot(snapshot)
    except DatabaseError:
        # The validator can encounter private values. Do not echo exceptions or raw rows.
        issue("database", 0, "snapshot_invalid")
    else:
        runs = {row["id"]: row for row in snapshot.tables["ingestion_runs"]}
        decisions = {row["queue_id"]: row for row in snapshot.tables["review_decisions"]}
        pending = [r for r in snapshot.tables["verification_queue"] if r["id"] not in decisions]
        inbox_counts = Counter()
        for table, positions in snapshot.row_positions.items():
            if (
                table not in HEADERS
                or len(positions) != len(snapshot.tables[table])
                or any(type(p) is not int or p < 1 for p in positions)
                or any(a >= b for a, b in zip(positions, positions[1:], strict=False))
            ):
                issue(table, 0, "physical_positions_invalid")
        for index, row in enumerate(snapshot.tables["ingestion_runs"]):
            try:
                if not re.fullmatch(r"[a-f0-9]{64}", row["payload_hash"]):
                    raise ValueError
                parse_json(row["report_json"])
                timestamp(row["created_at"])
            except (ValueError, TypeError):
                issue("ingestion_runs", index, "receipt_invalid")
        for index, row in enumerate(snapshot.tables["verification_queue"]):
            try:
                if (
                    row["request_id"] not in runs
                    or not row["item_index"].isascii()
                    or not row["item_index"].isdigit()
                    or row["reason"] not in QUEUE_REASONS
                    or not isinstance(parse_json(row["payload_json"]), dict)
                    or not isinstance(parse_json(row["errors_json"]), list)
                ):
                    raise ValueError
                timestamp(row["created_at"])
            except (ValueError, TypeError, AttributeError):
                issue("verification_queue", index, "queue_journal_invalid")
        for index, row in enumerate(snapshot.tables["change_history"]):
            try:
                before = parse_json(row["before_json"])
                after = parse_json(row["after_json"])
                if (
                    row["request_id"] not in runs
                    or row["action"]
                    not in {
                        "create",
                        "queue",
                        "update",
                        "restore",
                        "review",
                        "reject",
                        "backup_restore",
                    }
                    or row["table"] not in HEADERS
                    and row["table"] != "database"
                    or before is not None
                    and not isinstance(before, dict)
                    or not isinstance(after, dict)
                    or not row["entity_id"]
                ):
                    raise ValueError
                timestamp(row["created_at"])
            except (ValueError, TypeError):
                issue("change_history", index, "history_journal_invalid")
        for index, row in enumerate(snapshot.tables["inbox"]):
            try:
                if len(row["payload_json"]) > 40_000:
                    raise ValueError
                payload = parse_json(row["payload_json"])
                if not isinstance(payload, dict) or payload.get("request_id") != row["request_id"]:
                    raise ValueError
                if "action" in payload:
                    OPERATION_ADAPTER.validate_python(payload)
                else:
                    Batch.model_validate(payload)
                receipt = runs.get(row["request_id"])
                if (
                    receipt
                    and receipt["payload_hash"]
                    != hashlib.sha256(dumps(payload).encode()).hexdigest()
                ):
                    inbox_counts["conflict"] += 1
                    issue("inbox", index, "request_payload_conflict")
                else:
                    inbox_counts["processed" if receipt else "unprocessed"] += 1
            except (ValueError, TypeError, ValidationError):
                inbox_counts["invalid"] += 1
                issue("inbox", index, "inbox_envelope_invalid", "warning")
        report["verification_queue"] = {
            "pending": len(pending),
            "resolved": len(decisions),
            "pending_by_reason": dict(
                sorted(
                    Counter(
                        r["reason"] if r["reason"] in QUEUE_REASONS else "unknown" for r in pending
                    ).items()
                )
            ),
        }
        report["inbox"] = {
            key: inbox_counts[key] for key in ("unprocessed", "processed", "invalid", "conflict")
        }
    report.update(
        healthy=not any(i["severity"] == "error" for i in issues),
        issue_count=len(issues),
        error_count=sum(i["severity"] == "error" for i in issues),
        warning_count=sum(i["severity"] == "warning" for i in issues),
        issues=issues[:detail_limit],
        issues_truncated=len(issues) > detail_limit,
        limitations=[
            "점검 중·점검 후의 외부 편집을 차단하는 트랜잭션 격리는 없습니다.",
            "출처 URL의 실제 내용과 과거 이력의 사실관계는 자동 확인하지 않습니다.",
            "입력함은 봉투 형식만 검사하며 개별 항목/변경 가능 여부는 dry-run에서 검사합니다.",
        ],
    )
    return report
