"""Validated creates, registered-region classification and duplicate review."""

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from .classification import ClassificationError, prepare_item, resolve_item
from .duplicates import find_candidates
from .models import ENTITY_ADAPTER, Batch, Evidence, Region
from .provenance import validate_operational
from .repository import ConflictError, DatabaseError, Repository, Snapshot, planned_snapshot
from .schema import HEADERS, TABLE_BY_KIND, cell_value, dumps

LEVEL_ORDER = {
    "country": 0,
    "macro_region": 1,
    "administrative_area": 2,
    "city": 3,
    "neighborhood": 4,
}


def stable_id(prefix: str, key: str) -> str:
    return f"{prefix}_{uuid5(NAMESPACE_URL, 'personal-travel-db/' + prefix + '/' + key).hex}"


def normalize(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def identity(entity) -> str:
    data = entity.model_dump(mode="json")
    key = [data["kind"], data["region_id"], normalize(data["name"])]
    for field in (
        "address",
        "level",
        "country_code",
        "target_id",
        "budget_type",
        "experience_type",
        "transport_type",
        "duration_days",
        "people",
        "origin_region_id",
        "destination_region_id",
    ):
        if field in data:
            value = data[field]
            key.append(normalize(value) if isinstance(value, str) else value)
    return dumps(key)


def source_row(evidence: Evidence) -> dict:
    data = evidence.model_dump(mode="json")
    return {"id": stable_id("src", data["url"]), **data}


def entity_row(entity, now: str) -> dict:
    data = entity.model_dump(mode="json")
    data["id"] = data["id"] or stable_id(entity.kind, identity(entity))
    source_ids = sorted({source_row(s)["id"] for s in entity.sources})
    data["sources"] = source_ids
    data["source_url"] = min((str(s.url) for s in entity.sources), default=None)
    data["category"] = next(
        (
            data[k]
            for k in ("level", "food_type", "experience_type", "transport_type", "budget_type")
            if k in data
        ),
        entity.kind,
    )
    data["created_at"] = now
    data["updated_at"] = now
    return {key: cell_value(value) for key, value in data.items()}


def decode_entity(row: dict, sources: dict):
    """Restore typed records from flat Sheets cells; refuse damaged data."""
    data = {
        k: v or None
        for k, v in row.items()
        if k not in {"category", "source_url", "created_at", "updated_at"}
    }
    data["sources"] = [
        {k: v or None for k, v in sources[source_id].items() if k != "id"}
        for source_id in json.loads(row["sources"])
    ]
    for field in (
        "aliases",
        "ingredients",
        "features",
        "representative_region_ids",
        "signature_food_ids",
        "signature_menus",
        "price",
        "visited",
    ):
        if field in data and data[field] is not None:
            data[field] = json.loads(data[field])
    return ENTITY_ADAPTER.validate_python(data)


def check_references(entity, existing: dict):
    references = []
    if entity.region_id:
        references.append((entity.region_id, "region"))
    if isinstance(entity, Region) and entity.parent_region_id:
        parent = existing.get(entity.parent_region_id)
        if parent is not None:
            if parent.kind != "region":
                raise ValueError("부모 ID는 지역 항목이어야 합니다")
            if LEVEL_ORDER[parent.level] >= LEVEL_ORDER[entity.level]:
                raise ValueError("부모 지역은 더 상위 수준이어야 합니다")
            if parent.country_code != entity.country_code:
                raise ValueError("부모 지역과 국가 코드가 다릅니다")
    references.extend((r, "region") for r in getattr(entity, "representative_region_ids", []))
    references.extend((r, "food") for r in getattr(entity, "signature_food_ids", []))
    for field in ("origin_region_id", "destination_region_id"):
        if getattr(entity, field, None):
            references.append((getattr(entity, field), "region"))
    if entity.kind == "rating":
        references.append((entity.target_id, None))
    for target_id, kind in references:
        target = existing.get(target_id)
        if target is None or (kind and target.kind != kind):
            raise ValueError(f"참조 대상 {target_id}가 없거나 종류가 다릅니다")
        if entity.kind == "rating" and target_id == entity.target_id:
            if target.kind == "rating" or target.region_id != entity.region_id:
                raise ValueError("개인 평가 대상/지역이 일치하지 않습니다")


def validate_snapshot(snapshot: Snapshot) -> tuple[dict, dict]:
    if set(snapshot.tables) != set(HEADERS):
        raise DatabaseError("DB 탭 구성이 스키마와 다릅니다")
    ids = set()
    for table, rows in snapshot.tables.items():
        table_ids = set()
        for row in rows:
            if set(row) != set(HEADERS[table]):
                raise DatabaseError(f"{table} 행의 컬럼이 스키마와 다릅니다")
            key = row.get("id", row.get("request_id"))
            if not key or key in table_ids or (table != "inbox" and key in ids):
                raise DatabaseError(f"{table}에 누락/중복 ID가 있습니다")
            table_ids.add(key)
            # Inbox request_id may also exist in ingestion_runs after processing.
            if table != "inbox":
                ids.add(key)
    try:
        sources = {row["id"]: row for row in snapshot.tables["sources"]}
        for row in sources.values():
            evidence = Evidence.model_validate({k: v or None for k, v in row.items() if k != "id"})
            if row["id"] != source_row(evidence)["id"]:
                raise ValueError("출처 ID가 URL과 일치하지 않습니다")
        for row in snapshot.tables["ingestion_runs"]:
            receipt = json.loads(row["report_json"])
            if not isinstance(receipt, dict) or receipt.get("request_id") != row["id"]:
                raise ValueError("처리 영수증이 손상되었습니다")
        entities = {}
        by_identity = {}
        for kind, table in TABLE_BY_KIND.items():
            for row in snapshot.tables[table]:
                entity = decode_entity(row, sources)
                if entity.kind != kind or entity.verification_status != "verified":
                    raise ValueError("본 테이블에 잘못된 종류/미검증 항목이 있습니다")
                if not row["created_at"] or not row["updated_at"]:
                    raise ValueError("생성/수정일이 누락되었습니다")
                created = datetime.fromisoformat(row["created_at"])
                updated = datetime.fromisoformat(row["updated_at"])
                if created.tzinfo is None or updated.tzinfo is None or created > updated:
                    raise ValueError("생성/수정일 형식이 잘못되었습니다")
                canonical = entity_row(entity, row["created_at"])
                if (
                    row["category"] != canonical["category"]
                    or row["source_url"] != canonical["source_url"]
                ):
                    raise ValueError("분류/출처 요약이 모델과 다릅니다")
                key = identity(entity)
                if key in by_identity:
                    raise ValueError("본 테이블에 중복 식별키가 있습니다")
                by_identity[key] = entity.id
                entities[entity.id] = entity
        for entity in entities.values():
            check_references(entity, entities)
        validate_operational(snapshot, entities, sources)
        return entities, by_identity
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DatabaseError("기존 DB 행/참조가 손상되었습니다. 쓰기를 중단합니다") from exc


def comparable(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in {"id", "created_at", "updated_at"}}


def validation_errors(exc: ValidationError) -> list[dict]:
    # No original input echoed; raw item is preserved only in the quarantine payload.
    return [
        {"loc": list(e["loc"]), "type": e["type"], "message": e["msg"]}
        for e in exc.errors(include_input=False, include_url=False, include_context=False)
    ]


class IngestionService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def ingest(self, payload: dict, *, dry_run: bool = True) -> dict:
        batch = Batch.model_validate(payload)
        if len(dumps(payload).encode()) > 1_000_000:
            raise DatabaseError("입력 요청이 큽니다. 1 MB 이하로 나누세요")
        if any(len(dumps(raw)) > 40_000 for raw in batch.items):
            raise DatabaseError("항목 JSON은 Sheets 셀 제한을 위해 40,000자 이하여야 합니다")
        payload_hash = hashlib.sha256(dumps(payload).encode()).hexdigest()
        snapshot = self.repository.read()
        entities, by_identity = validate_snapshot(snapshot)
        receipt = next(
            (r for r in snapshot.tables["ingestion_runs"] if r["id"] == batch.request_id), None
        )
        if receipt:
            if receipt["payload_hash"] != payload_hash:
                raise ConflictError("같은 request_id에 다른 데이터가 입력되었습니다")
            report = json.loads(receipt["report_json"])
            return {**report, "dry_run": dry_run, "replayed": True, "committed": True}
        now = datetime.now(timezone.utc).isoformat()
        additions = {table: [] for table in HEADERS}
        report = {
            "request_id": batch.request_id,
            "dry_run": dry_run,
            "replayed": False,
            "committed": False,
            "created": 0,
            "skipped": 0,
            "queued": 0,
            "results": [],
        }
        current_rows = {
            row["id"]: row for table in TABLE_BY_KIND.values() for row in snapshot.tables[table]
        }
        source_ids = {r["id"] for r in snapshot.tables["sources"]}
        classifications = {}

        def history(entity_id, table, action, before, after, index):
            additions["change_history"].append(
                {
                    "id": stable_id("hist", f"{batch.request_id}/{index}/{action}"),
                    "request_id": batch.request_id,
                    "entity_id": entity_id,
                    "table": table,
                    "action": action,
                    "before_json": dumps(before),
                    "after_json": dumps(after),
                    "created_at": now,
                }
            )

        def queue(index, reason, errors, target_id=None, before=None, after=None, candidates=None):
            if candidates:
                errors = [*errors, {"duplicate_candidates": candidates}]
            queue_id = stable_id("queue", f"{batch.request_id}/{index}")
            row = {
                "id": queue_id,
                "request_id": batch.request_id,
                "item_index": index,
                "reason": reason,
                "target_id": target_id,
                "payload_json": dumps(batch.items[index]),
                "errors_json": dumps(errors),
                "created_at": now,
            }
            additions["verification_queue"].append(row)
            history(target_id or queue_id, "verification_queue", "queue", before, row, index)
            result = {
                "item_index": index,
                "status": "queued",
                "id": target_id,
                "queue_id": queue_id,
                "reason": reason,
                "errors": errors,
            }
            if before is not None:
                result["changes"] = {
                    k: {"before": before.get(k), "after": after.get(k)}
                    for k in comparable(after)
                    if before.get(k) != after.get(k)
                }
            report["results"].append(result)
            if candidates:
                result["duplicate_candidates"] = candidates
            report["queued"] += 1

        valid = []
        for index, raw in enumerate(batch.items):
            try:
                if batch.schema_version == "1.1":
                    prepared, rules = prepare_item(raw)
                    valid.append((index, prepared, rules))
                    continue
                entity = ENTITY_ADAPTER.validate_python(raw)
            except ClassificationError as exc:
                queue(index, "classification_error", [exc.detail()])
                continue
            except ValidationError as exc:
                queue(index, "validation_error", validation_errors(exc))
                continue
            if entity.verification_status != "verified":
                queue(index, "needs_verification", [])
                continue
            valid.append((index, entity.model_dump(mode="json"), []))

        def order(pair):
            _, data, _ = pair
            kinds = list(TABLE_BY_KIND)
            return (kinds.index(data["kind"]), LEVEL_ORDER.get(data.get("level"), 0))

        for index, data, rules in sorted(valid, key=order):
            try:
                if batch.schema_version == "1.1":
                    data, classification = resolve_item(data, entities, rules)
                    classifications[index] = classification
                entity = ENTITY_ADAPTER.validate_python(data)
            except ClassificationError as exc:
                queue(index, "classification_error", [exc.detail()])
                continue
            except ValidationError as exc:
                queue(index, "validation_error", validation_errors(exc))
                continue
            if entity.verification_status != "verified":
                queue(index, "needs_verification", [])
                continue
            row = entity_row(entity, now)
            entity_id = row["id"]
            table = TABLE_BY_KIND[entity.kind]
            key = identity(entity)
            duplicate_id = by_identity.get(key)
            if entity_id in entities and entities[entity_id].kind != entity.kind:
                queue(index, "id_conflict", ["기존 ID의 종류가 다릅니다"], entity_id)
                continue
            if duplicate_id and entity.id and duplicate_id != entity.id:
                queue(
                    index,
                    "duplicate_candidate",
                    [f"같은 식별키의 ID: {duplicate_id}"],
                    duplicate_id,
                    candidates=[
                        {
                            "id": duplicate_id,
                            "score": 1.0,
                            "policy_version": "1",
                            "evidence": [{"signal": "identity_exact"}],
                        }
                    ],
                )
                continue
            if duplicate_id:
                entity_id = duplicate_id
                row["id"] = duplicate_id
            if entity_id in current_rows:
                before = current_rows[entity_id]
                if comparable(before) == comparable(row):
                    report["skipped"] += 1
                    report["results"].append(
                        {"item_index": index, "status": "skipped", "id": entity_id}
                    )
                else:
                    queue(index, "change_candidate", [], entity_id, before, row)
                continue
            # Prevent cross-table IDs colliding with any operational record/request ID.
            used_ids = {r["id"] for rows in snapshot.tables.values() for r in rows if "id" in r}
            if entity_id in used_ids or entity_id == batch.request_id:
                queue(index, "id_conflict", ["이미 사용 중인 ID입니다"], entity_id)
                continue
            try:
                check_references(entity, entities)
            except ValueError as exc:
                queue(index, "reference_error", [str(exc)], entity_id)
                continue
            candidates = find_candidates(entity, entities)
            if candidates:
                queue(index, "duplicate_candidate", [], candidates[0]["id"], candidates=candidates)
                continue
            for evidence in entity.sources:
                source = source_row(evidence)
                if source["id"] not in source_ids:
                    additions["sources"].append(source)
                    source_ids.add(source["id"])
            additions[table].append(row)
            history(entity_id, table, "create", None, row, index)
            entities[entity_id] = entity.model_copy(update={"id": entity_id})
            by_identity[key] = entity_id
            current_rows[entity_id] = row
            report["created"] += 1
            report["results"].append(
                {"item_index": index, "status": "created", "id": entity_id, "table": table}
            )
        for result in report["results"]:
            if result["item_index"] in classifications:
                result["classification"] = classifications[result["item_index"]]
        report["results"].sort(key=lambda r: r["item_index"])
        report["committed"] = not dry_run
        additions["ingestion_runs"].append(
            {
                "id": batch.request_id,
                "payload_hash": payload_hash,
                "report_json": dumps(report),
                "created_at": now,
            }
        )
        # Validate exactly the same final state and cell limits in dry-run and apply.
        for rows in additions.values():
            for row in rows:
                if any(len(cell_value(v)) > 45_000 for v in row.values()):
                    raise DatabaseError("생성된 셀 내용이 큽니다. 요청을 작은 배치로 나누세요")
        planned = planned_snapshot(snapshot, additions)
        validate_snapshot(planned)
        if not dry_run:
            self.repository.commit(additions, snapshot)
        return report
