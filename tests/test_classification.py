import json
from copy import deepcopy
from pathlib import Path

import pytest

from travel_db.classification import ClassificationError, prepare_item, resolve_item
from travel_db.cli import validate_input
from travel_db.models import ENTITY_ADAPTER, INPUT_ADAPTER
from travel_db.service import IngestionService, validate_snapshot


@pytest.fixture
def registered(repository, sample):
    sample["items"][0].update(local_name="テスト国", aliases=["Sample Country", "테스트국"])
    sample["items"][1].update(local_name="サンプル市", aliases=["Sample City", "샘플시"])
    IngestionService(repository).ingest(sample, dry_run=False)
    return validate_snapshot(repository.read())[0]


def classify(raw, registered):
    data, rules = prepare_item(raw)
    data, report = resolve_item(data, registered, rules)
    return ENTITY_ADAPTER.validate_python(data), report


def research_item(sample, **hints):
    item = deepcopy(sample["items"][-1])
    item.pop("kind")
    item.pop("region_id")
    return {**item, "category": "음식점", "country": "JP", "city": "Sample City", **hints}


@pytest.mark.parametrize(
    "city", ["Sample City", "sample city", "Ｓａｍｐｌｅ Ｃｉｔｙ", "샘플시", "サンプル市"]
)
def test_registered_aliases_names_and_unicode_resolve(sample, registered, city):
    entity, report = classify(research_item(sample, city=city), registered)
    assert entity.kind == "restaurant" and entity.region_id == "sample-city"
    assert report["country_id"] == "sample-country"
    assert report["city_id"] == "sample-city"
    assert report["rules"]


@pytest.mark.parametrize("country", ["JP", "jp", "テスト国", "Sample Country", "테스트국"])
def test_country_can_be_code_alias_or_local_name(sample, registered, country):
    entity, _ = classify(research_item(sample, country=country), registered)
    assert entity.region_id == "sample-city"


def test_unknown_region_is_not_created_or_guessed(sample, registered):
    with pytest.raises(ClassificationError, match="見つからない|찾을 수") as error:
        classify(research_item(sample, city="Never registered city"), registered)
    assert error.value.code == "region_not_found"
    assert len(registered) == 3


def test_same_name_cities_require_country_or_id(sample, registered):
    country = registered["sample-country"].model_copy(
        update={"id": "other-country", "country_code": "KR"}
    )
    city = registered["sample-city"].model_copy(
        update={
            "id": "other-city",
            "country_code": "KR",
            "region_id": "other-country",
            "parent_region_id": "other-country",
        }
    )
    registered.update({country.id: country, city.id: city})
    item = research_item(sample)
    item.pop("country")
    with pytest.raises(ClassificationError) as error:
        classify(item, registered)
    assert error.value.code == "ambiguous_region"
    assert error.value.candidates == ["other-city", "sample-city"]
    entity, _ = classify(research_item(sample, country="KR"), registered)
    assert entity.region_id == "other-city"


def test_explicit_id_and_country_hint_conflict(sample, registered):
    country = registered["sample-country"].model_copy(
        update={"id": "other-country", "country_code": "KR"}
    )
    registered[country.id] = country
    with pytest.raises(ClassificationError) as error:
        classify(research_item(sample, region_id="sample-city", country="KR"), registered)
    assert error.value.code == "region_conflict"


def test_explicit_region_id_disambiguates_shared_country_alias(sample, registered):
    country = registered["sample-country"].model_copy(
        update={"id": "other-country", "country_code": "KR"}
    )
    registered[country.id] = country
    entity, _ = classify(
        research_item(sample, region_id="sample-city", country="Sample Country"), registered
    )
    assert entity.region_id == "sample-city"


def test_neighborhood_hint_is_constrained_by_city(sample, registered):
    district = registered["sample-city"].model_copy(
        update={
            "id": "sample-neighborhood",
            "level": "neighborhood",
            "region_id": "sample-city",
            "parent_region_id": "sample-city",
            "aliases": ["Old District"],
        }
    )
    registered[district.id] = district
    entity, report = classify(research_item(sample, region="Old District"), registered)
    assert entity.region_id == district.id and report["city_id"] == "sample-city"


@pytest.mark.parametrize(
    "category,kind,field,value",
    [
        ("カフェ", "experience", "experience_type", "cafe"),
        ("관광지", "experience", "experience_type", "attraction"),
        ("기차역", "transport", "transport_type", "train_station"),
        ("숙박비", "budget", "budget_type", "lodging"),
        ("라멘", "food", "food_type", "ramen"),
    ],
)
def test_category_sets_kind_and_subtype(sample, registered, category, kind, field, value):
    item = research_item(sample, category=category)
    if kind not in {"experience", "transport"}:
        item.pop("address")
    if kind == "food":
        item.pop("price")
    entity, _ = classify(item, registered)
    assert entity.kind == kind and getattr(entity, field) == value


@pytest.mark.parametrize(
    "hints,code",
    [
        ({"kind": "restaurant", "category": "카페"}, "category_conflict"),
        (
            {"kind": "experience", "category": "카페", "experience_type": "shopping"},
            "category_conflict",
        ),
        ({"category": "Unknown taxonomy"}, "unknown_category"),
        ({"kind": "Unknown kind"}, "unknown_kind"),
        ({"category": None}, "ambiguous_kind"),
        (
            {"category": None, "experience_type": "cafe", "transport_type": "airport"},
            "ambiguous_kind",
        ),
    ],
)
def test_unknown_ambiguous_and_conflicting_categories_fail(sample, registered, hints, code):
    with pytest.raises(ClassificationError) as error:
        classify(research_item(sample, **hints), registered)
    assert error.value.code == code


def test_infer_from_unique_subtype_without_name_guessing(sample, registered):
    item = research_item(sample, category=None, experience_type="카페")
    entity, report = classify(item, registered)
    assert entity.kind == "experience" and entity.experience_type == "cafe"
    assert report["rules"][0]["rule"] == "subtype_field"


def test_existing_custom_food_type_is_not_rewritten(sample, registered):
    item = research_item(sample, category="음식", food_type="사용자 정의 음식 분류")
    item.pop("address")
    item.pop("price")
    entity, _ = classify(item, registered)
    assert entity.food_type == "사용자 정의 음식 분류"


def test_new_region_parent_and_country_come_from_registered_country(sample, registered):
    item = deepcopy(sample["items"][1])
    for field in ("kind", "region_id", "parent_region_id", "country_code", "level"):
        item.pop(field)
    item.update(id="new-city", name="새 테스트 도시", category="도시", country="JP")
    entity, _ = classify(item, registered)
    assert entity.parent_region_id == "sample-country" and entity.country_code == "JP"


def test_canonical_subtype_case_is_normalized_in_context(sample, registered):
    item = research_item(sample, category="예산", budget_type="FOOD")
    item.pop("address")
    entity, _ = classify(item, registered)
    assert entity.kind == "budget" and entity.budget_type == "food"


def test_punctuation_only_alias_cannot_resolve_region(sample, registered):
    city = registered["sample-city"]
    registered[city.id] = city.model_copy(update={"aliases": ["---"]})
    with pytest.raises(ClassificationError):
        classify(research_item(sample, city="+++"), registered)


def test_version_11_batch_resolves_new_regions_before_places(repository, sample):
    sample["schema_version"] = "1.1"
    sample["items"][1]["aliases"] = ["Sample City"]
    sample["items"][-1] = research_item(sample)
    sample["items"].reverse()
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["created"] == 3 and report["queued"] == 0
    entities, _ = validate_snapshot(repository.read())
    restaurant = next(e for e in entities.values() if e.kind == "restaurant")
    assert restaurant.region_id == "sample-city"
    assert report["results"][0]["classification"]["city_id"] == "sample-city"


def test_version_11_resolved_input_repeats_without_duplicates(repository, sample, registered):
    payload = {"schema_version": "1.1", "request_id": "aliases-1", "items": [research_item(sample)]}
    service = IngestionService(repository)
    report = service.ingest(payload, dry_run=False)
    assert report["skipped"] == 1
    payload["request_id"] = "aliases-2"
    payload["items"][0]["city"] = "サンプル市"
    assert service.ingest(payload, dry_run=False)["skipped"] == 1
    assert len(repository.tables["restaurants"]) == 1


def test_classification_failure_queue_preserves_raw_input(repository, sample, registered):
    raw = research_item(sample, city="Unknown city")
    payload = {"schema_version": "1.1", "request_id": "unknown-city", "items": [raw]}
    service = IngestionService(repository)
    before = repository.read().digest
    report = service.ingest(payload)
    assert report["results"][0]["reason"] == "classification_error"
    assert repository.read().digest == before
    service.ingest(payload, dry_run=False)
    assert json.loads(repository.tables["verification_queue"][0]["payload_json"]) == raw
    assert len(repository.tables["restaurants"]) == 1


def test_schema_accepts_both_contracts_and_input_validator_is_explicit(sample):
    INPUT_ADAPTER.validate_python(sample)
    payload = {
        "schema_version": "1.1",
        "request_id": "input-only",
        "items": [research_item(sample)],
    }
    INPUT_ADAPTER.validate_python(payload)
    report = validate_input(payload)
    assert report["valid"] and report["results"][0]["validation_mode"] == "input_only"


def test_input_only_validation_checks_taxonomy(sample):
    payload = {
        "schema_version": "1.1",
        "request_id": "bad-taxonomy",
        "items": [research_item(sample, category="Unregistered category")],
    }
    assert validate_input(payload)["valid"] is False


def test_wrong_kind_specific_fields_still_fail_after_classification(repository, sample, registered):
    raw = research_item(sample, category="카페", signature_food_ids=["missing-food"])
    payload = {"schema_version": "1.1", "request_id": "wrong-fields", "items": [raw]}
    result = IngestionService(repository).ingest(payload, dry_run=False)["results"][0]
    assert result["reason"] == "validation_error"
    assert repository.tables["experiences"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"price": {"amount": "1000", "basis": "meal"}},
        {"sources": []},
        {"last_verified_on": None},
        {"researched_on": "9999-12-31"},
        {"latitude": 35.0},
    ],
)
def test_final_model_rules_still_quarantine_invalid_v11_facts(
    repository, sample, registered, changes
):
    payload = {
        "schema_version": "1.1",
        "request_id": "bad-v11-facts",
        "items": [research_item(sample, **changes)],
    }
    original = deepcopy(repository.tables["restaurants"])
    report = IngestionService(repository).ingest(payload, dry_run=False)
    assert report["results"][0]["reason"] == "validation_error"
    assert repository.tables["restaurants"] == original


def test_classification_example_full_flow_and_replay(repository):
    payload = json.loads(
        (Path(__file__).parents[1] / "examples/classification.json").read_text(encoding="utf-8")
    )
    service = IngestionService(repository)
    before = repository.read().digest
    dry = service.ingest(payload)
    assert (dry["created"], dry["queued"]) == (3, 2)
    assert repository.read().digest == before
    report = service.ingest(payload, dry_run=False)
    assert [r.get("reason") for r in report["results"][-2:]] == [
        "duplicate_candidate",
        "classification_error",
    ]
    assert len(repository.tables["restaurants"]) == 1
    assert len(repository.tables["verification_queue"]) == 2
    assert service.ingest(payload, dry_run=False)["replayed"] is True
    assert repository.commits == 1


def test_published_json_schema_matches_runtime():
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/input.schema.json").read_text(encoding="utf-8")
    )
    assert schema == INPUT_ADAPTER.json_schema()


def test_v10_does_not_accept_v11_hints(repository, sample):
    sample["items"][-1]["city"] = "Sample City"
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["results"][-1]["reason"] == "validation_error"
