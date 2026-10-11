"""Append-only field evidence, keyed by the exact entity revision it describes."""

import hashlib
import json
import re
from datetime import date, datetime

from .schema import dumps

CONTROL_FIELDS = {
    "id",
    "kind",
    "sources",
    "researched_on",
    "last_verified_on",
    "verification_status",
}
PERSONAL_FIELDS = {"taste_fit", "visited", "satisfaction", "notes"}


def row_revision(row: dict) -> str:
    return hashlib.sha256(dumps(row).encode()).hexdigest()


def current_provenance(snapshot, row: dict) -> dict:
    revision = row_revision(row)
    return {
        r["field"]: {
            "source_ids": json.loads(r["source_ids"]),
            "verified_on": r["verified_on"],
            "basis": r["basis"],
        }
        for r in snapshot.tables["field_sources"]
        if r["entity_id"] == row["id"] and r["entity_revision"] == revision
    }


def entity_hash(row: dict, provenance: dict) -> str:
    return hashlib.sha256(dumps({"entity": row, "field_sources": provenance}).encode()).hexdigest()


def timestamp(value: str):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("UTC 오프셋이 있는 기록 시각이 필요합니다")
    return result


def validate_operational(snapshot, entities, sources):
    """Refuse corrupt decision/evidence records rather than silently losing review state."""
    queues = {r["id"]: r for r in snapshot.tables["verification_queue"]}
    runs = {r["id"] for r in snapshot.tables["ingestion_runs"]}
    resolved = set()
    for r in snapshot.tables["review_decisions"]:
        if (
            r["queue_id"] not in queues
            or r["queue_id"] in resolved
            or r["decision"] not in {"approve", "reject"}
            or r["request_id"] not in runs
            or not r["reviewer"]
            or not r["reason"]
        ):
            raise ValueError("승인/거부 기록이 손상되었습니다")
        if r["decision"] == "approve":
            if r["entity_id"] not in entities:
                raise ValueError("승인 대상이 없습니다")
        elif r["entity_id"]:
            raise ValueError("거부 기록에 승인 대상이 있습니다")
        timestamp(r["created_at"])
        resolved.add(r["queue_id"])
    pairs = set()
    current_rows = {r["id"]: r for rows in snapshot.tables.values() for r in rows if "kind" in r}
    for r in snapshot.tables["field_sources"]:
        entity = entities.get(r["entity_id"])
        pair = (r["entity_id"], r["entity_revision"], r["field"])
        source_ids = json.loads(r["source_ids"])
        if (
            entity is None
            or r["field"] not in type(entity).model_fields
            or r["field"] in CONTROL_FIELDS
            or pair in pairs
            or not re.fullmatch(r"[a-f0-9]{64}", r["entity_revision"])
            or not isinstance(source_ids, list)
            or any(not isinstance(s, str) or s not in sources for s in source_ids)
            or len(source_ids) != len(set(source_ids))
            or r["request_id"] not in runs
        ):
            raise ValueError("필드 출처 기록이 손상되었습니다")
        if r["basis"] == "personal":
            if entity.kind != "rating" or r["field"] not in PERSONAL_FIELDS or source_ids:
                raise ValueError("개인 평가 출처 구분이 잘못되었습니다")
        elif r["basis"] != "sources" or not source_ids:
            raise ValueError("필드 근거가 없습니다")
        if date.fromisoformat(r["verified_on"]) > date.today():
            raise ValueError("필드 확인일은 미래일 수 없습니다")
        timestamp(r["created_at"])
        if r["entity_revision"] == row_revision(current_rows[entity.id]):
            if not set(source_ids) <= set(json.loads(current_rows[entity.id]["sources"])):
                raise ValueError("현재 항목의 출처에 필드 근거가 없습니다")
            if date.fromisoformat(r["verified_on"]) > entity.last_verified_on:
                raise ValueError("필드 확인일이 항목의 최종 확인일보다 늦습니다")
        pairs.add(pair)
