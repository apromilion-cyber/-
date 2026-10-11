"""Explicit offline benchmark using fictional in-memory fixtures, never Sheets/Secrets."""

import argparse
import json
import platform
import time
from copy import deepcopy
from pathlib import Path

from conftest import MemoryRepository

from travel_db.audit import audit_database
from travel_db.models import ENTITY_ADAPTER
from travel_db.research import plan_research
from travel_db.schema import dumps
from travel_db.service import IngestionService, entity_row, stable_id, validate_snapshot


def scale_fixture(count):
    if not 50 <= count <= 50_000:
        raise ValueError("가상 음식점 수는 50~50000이어야 합니다")
    repository = MemoryRepository()
    sample = json.loads((Path(__file__).parents[1] / "examples/sample.json").read_text())
    IngestionService(repository).ingest(sample, dry_run=False)
    template = sample["items"][2]
    fixture_items = [template]
    timestamp = "2026-01-01T00:00:00+00:00"
    # Construct a fictional imported baseline with the real models/serialization.
    # Fixture construction is not an API ingestion throughput measurement.
    for index in range(1, count):
        item = {
            **template,
            "id": f"fictional-scale-{index}",
            "name": f"가상 규모 식당 {index:06d}",
            "address": f"가상 규모 주소 {index:06d}",
        }
        row = entity_row(ENTITY_ADAPTER.validate_python(item), timestamp)
        repository.tables["restaurants"].append(row)
        repository.tables["change_history"].append(
            {
                "id": stable_id("hist", f"fictional-scale/{index}"),
                "request_id": sample["request_id"],
                "entity_id": row["id"],
                "table": "restaurants",
                "action": "create",
                "before_json": "null",
                "after_json": dumps(row),
                "created_at": timestamp,
            }
        )
        if len(fixture_items) < 50:
            fixture_items.append(item)
    return repository, sample, fixture_items


def benchmark(count):
    repository, sample, existing = scale_fixture(count)
    measurements = {}

    def measure(name, action):
        started = time.perf_counter()
        result = action()
        measurements[name] = round(time.perf_counter() - started, 6)
        return result

    snapshot = repository.read()
    before = snapshot.digest
    measure("validate_snapshot_seconds", lambda: validate_snapshot(snapshot))
    audit = measure("audit_seconds", lambda: audit_database(repository))
    assert audit["healthy"] and audit["issue_count"] == 0
    plan = measure(
        "research_plan_seconds",
        lambda: plan_research(
            repository,
            {
                "schema_version": "1.0",
                "request_id": "fictional-scale-plan",
                "region_id": "sample-city",
                "category": "음식점",
                "count": 50,
            },
        ),
    )
    assert plan["existing_count"] == count and len(plan["existing_items"]) == 50
    repeated = measure(
        "repeat_50_dry_run_seconds",
        lambda: IngestionService(repository).ingest(
            {"schema_version": "1.0", "request_id": "fictional-repeat", "items": existing}
        ),
    )
    assert repeated["skipped"] == 50 and repeated["created"] == repeated["queued"] == 0
    new_item = deepcopy(sample["items"][2])
    new_item["name"] = "새로운 분리 검증 장소 (가상)"
    new_item["address"] = "완전히 다른 가상 위치"
    proposed = measure(
        "one_new_item_dry_run_seconds",
        lambda: IngestionService(repository).ingest(
            {"schema_version": "1.0", "request_id": "fictional-new", "items": [new_item]}
        ),
    )
    assert proposed["created"] == 1 and proposed["queued"] == 0
    assert repository.read().digest == before and repository.commits == 1
    return {
        "mode": "offline_fictional_memory",
        "python": platform.python_version(),
        "restaurant_count": count,
        "row_counts": audit["row_counts"],
        "measurements": measurements,
        "checks_passed": True,
        "unchanged": True,
        "limitations": "Google API 지연·할당량·실제 대량 쓰기·동시 편집은 측정하지 않습니다.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="인증 없는 가상 데이터 성능 측정")
    parser.add_argument("--restaurants", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.restaurants), ensure_ascii=False, indent=2))
