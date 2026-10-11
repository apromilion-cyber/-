"""Read-only planning for an explicit user request; no web collection or scheduling."""

from typing import Annotated, Literal

from pydantic import Field

from .classification import ancestors, category_rule, resolve_scope
from .models import MODEL_BY_KIND, Identifier, StrictModel, Text
from .repository import ConflictError
from .service import validate_snapshot


class ResearchRequest(StrictModel):
    schema_version: Literal["1.0"]
    request_id: Identifier
    category: Text
    count: Annotated[int, Field(strict=True, ge=1, le=500)]
    region_id: Identifier | None = None
    country: Text | None = None
    city: Text | None = None
    region: Text | None = None
    existing_limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 50
    offset: Annotated[int, Field(strict=True, ge=0)] = 0
    expected_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


def plan_research(repository, payload: dict) -> dict:
    request = ResearchRequest.model_validate(payload)
    snapshot = repository.read()
    if request.expected_digest and request.expected_digest != snapshot.digest:
        raise ConflictError("조사 계획 페이지 사이 DB가 변경되었습니다. 처음부터 다시 조회하세요")
    entities, _ = validate_snapshot(snapshot)
    kind, field, subtype = category_rule(request.category)
    scope = resolve_scope(
        {k: getattr(request, k) for k in ("region_id", "country", "city", "region")}, entities
    )
    matched = []
    for entity in entities.values():
        in_scope = (entity.kind == "region" and entity.id == scope["region_id"]) or any(
            a.id == scope["region_id"] for a in ancestors(entity.region_id, entities)
        )
        if (
            entity.kind == kind
            and in_scope
            and (field is None or getattr(entity, field) == subtype)
        ):
            matched.append(
                {
                    "id": entity.id,
                    "name": entity.name,
                    "local_name": entity.local_name,
                    "region_id": entity.region_id,
                    "last_verified_on": entity.last_verified_on.isoformat(),
                    "source_urls": [str(s.url) for s in entity.sources],
                }
            )
    hints = {"kind": kind, "region_id": scope["region_id"]}
    for item in matched:
        entity = entities[item["id"]]
        for location_field in ("address", "latitude", "longitude"):
            if location_field in type(entity).model_fields:
                item[location_field] = getattr(entity, location_field)
    if field:
        hints[field] = subtype
    return {
        "request_id": request.request_id,
        "requested_count": request.count,
        "read_only": True,
        "scope": scope,
        "classification_hints": hints,
        "existing_count": len(matched),
        "existing_items": sorted(matched, key=lambda r: r["id"])[
            request.offset : request.offset + request.existing_limit
        ],
        "next_offset": request.offset + request.existing_limit
        if request.offset + request.existing_limit < len(matched)
        else None,
        "snapshot_digest": snapshot.digest,
        "required_item_fields": sorted(
            f for f, info in MODEL_BY_KIND[kind].model_fields.items() if info.is_required()
        ),
        "instructions": [
            "사용자가 요청한 범위/건수만 조사하고 실제 출처와 확인일을 기록하세요.",
            "기존 ID·주소·현지명과 비교하고 변경 후보는 검토 대기로 제출하세요.",
            "확인하지 못한 값은 null, 미검증 항목은 unverified로 작성하세요.",
            "조사 JSON 1.0/1.1을 ingest 또는 inbox에 제출하고 dry-run을 확인하세요.",
        ],
        "input_contract": "docs/input.schema.json",
    }
