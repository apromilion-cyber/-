"""Approval, rejection, guarded updates and restoration in one Sheets batch."""

import hashlib
import json
from datetime import datetime, timezone

from pydantic import TypeAdapter

from .duplicates import find_candidates
from .operations import OPERATION_ADAPTER, Approve, FieldProof, Reject, Restore
from .provenance import (
    CONTROL_FIELDS,
    PERSONAL_FIELDS,
    current_provenance,
    entity_hash,
    row_revision,
)
from .repository import ConflictError, DatabaseError, planned_snapshot
from .schema import HEADERS, TABLE_BY_KIND, dumps
from .service import (
    comparable,
    decode_entity,
    entity_row,
    identity,
    source_row,
    stable_id,
    validate_snapshot,
)


def entity_rows(snapshot):
    return {r["id"]: r for table in TABLE_BY_KIND.values() for r in snapshot.tables[table]}


def inspect_entity(repository, target_id: str) -> dict:
    snapshot = repository.read()
    validate_snapshot(snapshot)
    row = entity_rows(snapshot).get(target_id)
    if row is None:
        raise DatabaseError("대상 항목이 없습니다")
    provenance = current_provenance(snapshot, row)
    return {
        "id": target_id,
        "entity": row,
        "field_sources": provenance,
        "expected_hash": entity_hash(row, provenance),
    }


def pending_reviews(repository) -> dict:
    snapshot = repository.read()
    validate_snapshot(snapshot)
    decisions = {r["queue_id"]: r for r in snapshot.tables["review_decisions"]}
    rows = [r for r in snapshot.tables["verification_queue"] if r["id"] not in decisions]
    return {"pending": len(rows), "resolved": len(decisions), "rows": rows}


def build_provenance(entity, before_entity, previous, submitted):
    data = entity.model_dump(mode="json")
    old = before_entity.model_dump(mode="json") if before_entity else {}
    fields = set(type(entity).model_fields) - CONTROL_FIELDS
    if set(submitted) - fields:
        raise ValueError("field_sources는 항목의 실제 사실 필드만 지정할 수 있습니다")
    required = (
        {f for f in fields if old.get(f) != data.get(f)}
        if before_entity
        else {f for f in fields if data[f] is not None and data[f] != []}
    )
    source_urls = {str(s.url): source_row(s)["id"] for s in entity.sources}
    permitted = set(source_urls.values())
    required |= {f for f, p in previous.items() if not set(p["source_ids"]) <= permitted}
    if required - set(submitted):
        raise ValueError(
            "변경/신규 필드의 출처가 필요합니다: " + ", ".join(sorted(required - set(submitted)))
        )
    result = {f: dict(p) for f, p in previous.items()}
    for field, urls in submitted.items():
        urls = sorted(set(str(url) for url in urls))
        if not urls:
            if entity.kind != "rating" or field not in PERSONAL_FIELDS:
                raise ValueError("개인 평가 필드 외에는 빈 필드 출처를 사용할 수 없습니다")
            basis = "personal"
        else:
            if not set(urls) <= set(source_urls):
                raise ValueError("필드 출처 URL은 entity.sources에도 포함해야 합니다")
            basis = "sources"
        result[field] = {
            "source_ids": sorted(source_urls[u] for u in urls),
            "verified_on": entity.last_verified_on.isoformat(),
            "basis": basis,
        }
    return result


class ReviewService:
    def __init__(self, repository):
        self.repository = repository

    def execute(self, payload: dict, *, dry_run: bool = True) -> dict:
        operation = OPERATION_ADAPTER.validate_python(payload)
        if len(dumps(payload).encode()) > 1_000_000:
            raise ValueError("작업 JSON은 1 MB 이하여야 합니다")
        payload_hash = hashlib.sha256(dumps(payload).encode()).hexdigest()
        snapshot = self.repository.read()
        entities, by_identity = validate_snapshot(snapshot)
        rows = entity_rows(snapshot)
        receipt = next(
            (r for r in snapshot.tables["ingestion_runs"] if r["id"] == operation.request_id), None
        )
        if receipt:
            if receipt["payload_hash"] != payload_hash:
                raise ConflictError("같은 request_id에 다른 데이터가 입력되었습니다")
            return {
                **json.loads(receipt["report_json"]),
                "dry_run": dry_run,
                "replayed": True,
                "committed": True,
            }
        used_ids = {r["id"] for records in snapshot.tables.values() for r in records if "id" in r}
        if operation.request_id in used_ids:
            raise ConflictError("request_id가 기존 기록 ID와 충돌합니다")
        now = datetime.now(timezone.utc).isoformat()
        additions = {table: [] for table in HEADERS}
        updates = {}
        report = {
            "request_id": operation.request_id,
            "action": operation.action,
            "dry_run": dry_run,
            "replayed": False,
            "committed": not dry_run,
            "reviewer": operation.reviewer,
            "reason": operation.reason,
            "changes": {},
        }
        queue = None
        before_bundle = None
        after_bundle = None
        if isinstance(operation, (Approve, Reject)):
            queue = next(
                (r for r in snapshot.tables["verification_queue"] if r["id"] == operation.queue_id),
                None,
            )
            if queue is None:
                raise DatabaseError("검증 대기 ID가 없습니다")
            if any(r["queue_id"] == queue["id"] for r in snapshot.tables["review_decisions"]):
                raise ConflictError("이미 승인/거부한 대기 항목입니다")
            if (
                isinstance(operation, Approve)
                and queue["reason"] == "change_candidate"
                and (operation.mode != "update" or operation.target_id != queue["target_id"])
            ):
                raise ConflictError("변경 후보는 대기열의 기존 대상 ID에 update로 승인해야 합니다")
            report["queue_id"] = queue["id"]
        if isinstance(operation, Reject):
            report["status"] = "rejected"
            table, entity_id, history_action = "review_decisions", queue["id"], "reject"
        else:
            restoring = isinstance(operation, Restore)
            if restoring:
                history = next(
                    (
                        r
                        for r in snapshot.tables["change_history"]
                        if r["id"] == operation.history_id
                    ),
                    None,
                )
                if history is None or history["action"] not in {"update", "restore"}:
                    raise ValueError("update/restore 이력만 이전 값으로 복구할 수 있습니다")
                try:
                    original_before = json.loads(history["before_json"])
                    original_after = json.loads(history["after_json"])
                    for bundle in (original_before, original_after):
                        if (
                            not isinstance(bundle, dict)
                            or not isinstance(bundle["entity"], dict)
                            or not isinstance(bundle["field_sources"], dict)
                        ):
                            raise ValueError("history format")
                        history_row = bundle["entity"]
                        if set(history_row) != set(HEADERS[TABLE_BY_KIND[history_row["kind"]]]):
                            raise ValueError("history columns")
                        TypeAdapter(dict[str, FieldProof]).validate_python(bundle["field_sources"])
                    target_id = original_after["entity"]["id"]
                    if (
                        target_id != history["entity_id"]
                        or history["table"] != TABLE_BY_KIND[original_after["entity"]["kind"]]
                    ):
                        raise ValueError("history target")
                except (ValueError, KeyError, TypeError) as exc:
                    raise DatabaseError("복구 이력이 손상되었습니다") from exc
                report["restored_history_id"] = history["id"]
            else:
                target_id = operation.target_id
            creating = isinstance(operation, Approve) and operation.mode == "create"
            before = None if creating else rows.get(target_id)
            if not creating and before is None:
                raise DatabaseError("업데이트 대상 항목이 없습니다")
            previous = current_provenance(snapshot, before) if before else {}
            if before:
                if entity_hash(before, previous) != operation.expected_hash:
                    raise ConflictError(
                        "검토 이후 대상 값/출처가 변경되었습니다. inspect로 다시 확인하세요"
                    )
                before_bundle = {"entity": before, "field_sources": previous}
            if restoring:
                if before_bundle != {k: original_after[k] for k in ("entity", "field_sources")}:
                    raise ConflictError("복구할 이력 이후 항목이 변경되었습니다")
                try:
                    sources = {r["id"]: r for r in snapshot.tables["sources"]}
                    entity = decode_entity(original_before["entity"], sources)
                    provenance = original_before["field_sources"]
                    if (
                        entity.id != target_id
                        or entity.kind != before["kind"]
                        or original_before["entity"]["created_at"] != before["created_at"]
                    ):
                        raise ValueError("immutable identity")
                except (ValueError, KeyError, TypeError) as exc:
                    raise DatabaseError("복구할 이전 값이 손상되었습니다") from exc
            else:
                entity = operation.entity
                report["distinct_from"] = operation.distinct_from
                if isinstance(operation, Approve):
                    report["mode"] = operation.mode
                if entity.verification_status != "verified":
                    raise ValueError("검증된 항목만 승인/업데이트할 수 있습니다")
                if before and (entity.kind != before["kind"] or entity.id not in {None, target_id}):
                    raise ValueError("기존 ID/종류는 변경할 수 없습니다")
                before_entity = entities.get(target_id) if before else None
                provenance = build_provenance(
                    entity, before_entity, previous, operation.field_sources
                )
            row = entity_row(entity, now)
            if before:
                row["id"] = before["id"]
                row["created_at"] = before["created_at"]
            entity_id = row["id"]
            entity = entity.model_copy(update={"id": entity_id})
            table = TABLE_BY_KIND[entity.kind]
            if creating and (entity_id in used_ids or entity_id == operation.request_id):
                raise ConflictError(
                    "신규 승인 ID가 기존 기록과 충돌합니다. update 승인을 사용하세요"
                )
            duplicate_id = by_identity.get(identity(entity))
            if duplicate_id and duplicate_id != entity_id:
                raise ConflictError(
                    "동일 식별키의 항목이 이미 있습니다. 명시적으로 기존 항목을 업데이트하세요"
                )
            others = {key: value for key, value in entities.items() if key != entity_id}
            candidates = find_candidates(entity, others)
            report["duplicate_candidates"] = candidates
            if not restoring:
                unreviewed = {r["id"] for r in candidates} - set(operation.distinct_from)
                if unreviewed:
                    raise ConflictError(
                        "다른 항목으로 확인한 중복 후보 ID를 distinct_from에 지정하세요"
                    )
            elif candidates:
                # Historical restoration still validates exact identity/FKs; fuzzy suggestions
                # are reported, since the restored identity was already explicitly reviewed.
                report["historical_duplicate_candidates"] = candidates
            # Restore via a new revision: neither history nor old evidence is deleted.
            if before and comparable(row) == comparable(before) and provenance == previous:
                row = dict(before)
                report["status"] = "unchanged"
                history_action = "review"
            else:
                report["status"] = "created" if creating else "restored" if restoring else "updated"
                history_action = "create" if creating else "restore" if restoring else "update"
                report["changes"] = {
                    k: {"before": before.get(k) if before else None, "after": v}
                    for k, v in comparable(row).items()
                    if before is None or before.get(k) != v
                }
                if creating:
                    additions[table].append(row)
                else:
                    updates[table] = [row]
                for evidence in entity.sources:
                    source = source_row(evidence)
                    if source["id"] not in used_ids:
                        additions["sources"].append(source)
                        used_ids.add(source["id"])
                revision = row_revision(row)
                for field, value in sorted(provenance.items()):
                    additions["field_sources"].append(
                        {
                            "id": stable_id("field", f"{entity_id}/{revision}/{field}"),
                            "entity_id": entity_id,
                            "entity_revision": revision,
                            "field": field,
                            "source_ids": dumps(value["source_ids"]),
                            "basis": value["basis"],
                            "verified_on": value["verified_on"],
                            "request_id": operation.request_id,
                            "created_at": now,
                        }
                    )
            report["id"] = entity_id
            report["field_sources"] = {"before": previous, "after": provenance}
            report["expected_hash_after"] = entity_hash(row, provenance)
            after_bundle = {
                "entity": row,
                "field_sources": provenance,
                "reviewer": operation.reviewer,
                "reason": operation.reason,
            }
            if not restoring:
                after_bundle["distinct_from"] = operation.distinct_from
        if queue:
            decision = {
                "id": stable_id("decision", queue["id"]),
                "queue_id": queue["id"],
                "decision": operation.action,
                "entity_id": report.get("id", ""),
                "reviewer": operation.reviewer,
                "reason": operation.reason,
                "request_id": operation.request_id,
                "created_at": now,
            }
            additions["review_decisions"].append(decision)
            if isinstance(operation, Reject):
                after_bundle = decision
            report["decision_id"] = decision["id"]
        history_id = stable_id("hist", f"{operation.request_id}/operation")
        additions["change_history"].append(
            {
                "id": history_id,
                "request_id": operation.request_id,
                "entity_id": entity_id,
                "table": table,
                "action": history_action,
                "before_json": dumps(before_bundle),
                "after_json": dumps(after_bundle),
                "created_at": now,
            }
        )
        report["history_id"] = history_id
        additions["ingestion_runs"].append(
            {
                "id": operation.request_id,
                "payload_hash": payload_hash,
                "report_json": dumps(report),
                "created_at": now,
            }
        )
        validate_snapshot(planned_snapshot(snapshot, additions, updates))
        if not dry_run:
            self.repository.commit(additions, snapshot, updates=updates)
        return report
