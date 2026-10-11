from copy import deepcopy

import pytest

from travel_db.duplicates import DuplicatePolicy, distance_meters, find_candidates
from travel_db.models import ENTITY_ADAPTER
from travel_db.service import IngestionService, validate_snapshot


@pytest.fixture
def places(repository, sample):
    sample["items"][-1].update(
        id="sample-place",
        name="Sample Bistro",
        local_name="サンプル食堂",
        address="Test street 10",
        latitude=35.0,
        longitude=139.0,
    )
    IngestionService(repository).ingest(sample, dry_run=False)
    return validate_snapshot(repository.read())[0]


def incoming(sample, **changes):
    item = deepcopy(sample["items"][-1])
    item.update(id="new-place", **changes)
    return ENTITY_ADAPTER.validate_python(item)


def test_typo_with_same_address_has_explainable_candidate(sample, places):
    entity = incoming(sample, name="Sample Bistr0", local_name=None)
    candidates = find_candidates(entity, places)
    assert candidates[0]["id"] == "sample-place" and candidates[0]["score"] == 0.97
    signals = {e["signal"] for e in candidates[0]["evidence"]}
    assert {"name_similarity", "address_exact", "coordinate_distance"} <= signals


def test_cross_language_local_name_match(sample, places):
    candidates = find_candidates(incoming(sample, name="サンプル食堂", local_name=None), places)
    evidence = candidates[0]["evidence"][0]
    assert evidence["input_field"] == "name" and evidence["existing_field"] == "local_name"


def test_near_coordinates_find_changed_address(sample, places):
    entity = incoming(
        sample, name="Sample Bistr0", local_name=None, address="Another road 15", latitude=35.0005
    )
    assert find_candidates(entity, places)[0]["score"] == 0.97


def test_nearby_boundary_is_inclusive(sample, places):
    entity = incoming(
        sample, name="Sample Bistr0", local_name=None, address="Another road 15", latitude=35.0008
    )
    meters = distance_meters(entity, places["sample-place"])
    assert find_candidates(entity, places, DuplicatePolicy(nearby_meters=meters))
    assert not find_candidates(entity, places, DuplicatePolicy(nearby_meters=meters - 0.01))


def test_threshold_is_inclusive_and_configurable(sample, places):
    entity = incoming(sample, name="Sample Bistr0", local_name=None)
    assert find_candidates(entity, places, DuplicatePolicy(candidate_threshold=0.97))
    assert not find_candidates(entity, places, DuplicatePolicy(candidate_threshold=0.971))


def test_distinct_far_branches_are_not_merged(sample, places):
    entity = incoming(sample, address="Another street 50", latitude=35.01)
    assert not find_candidates(entity, places)


def test_coordinate_conflict_with_exact_address_requires_review(sample, places):
    entity = incoming(sample, latitude=35.01)
    assert find_candidates(entity, places)


def test_same_address_or_coordinates_alone_is_not_enough(sample, places):
    entity = incoming(sample, name="Museum gift shop", local_name=None)
    assert not find_candidates(entity, places)


def test_different_country_or_city_is_excluded(sample, places):
    places["other-country"] = places["sample-country"].model_copy(
        update={"id": "other-country", "country_code": "KR"}
    )
    places["other-city"] = places["sample-city"].model_copy(
        update={
            "id": "other-city",
            "region_id": "other-country",
            "parent_region_id": "other-country",
            "country_code": "KR",
        }
    )
    assert not find_candidates(incoming(sample, region_id="other-city"), places)


def test_neighborhood_and_city_share_matching_scope(sample, places):
    places["district"] = places["sample-city"].model_copy(
        update={
            "id": "district",
            "level": "neighborhood",
            "region_id": "sample-city",
            "parent_region_id": "sample-city",
        }
    )
    assert find_candidates(incoming(sample, region_id="district"), places)


def test_missing_coordinates_are_not_treated_as_zero(sample, places):
    entity = incoming(
        sample, name="Sample Bistr0", local_name=None, address=None, latitude=None, longitude=None
    )
    assert distance_meters(entity, places["sample-place"]) is None
    assert not find_candidates(entity, places)  # name similarity .92 is below name-only .94


def test_name_only_similarity_requires_strict_threshold(sample, places):
    original = places["sample-place"].model_copy(
        update={"name": "ABCDEFGHIJKLMNOPQRST", "local_name": None}
    )
    places[original.id] = original
    entity = incoming(
        sample,
        name="ABCDEFGHIJKLMNOPQRSX",
        local_name=None,
        address=None,
        latitude=None,
        longitude=None,
    )
    assert find_candidates(entity, places)[0]["score"] == 0.85
    assert not find_candidates(entity, places, DuplicatePolicy(name_only=0.951))


def test_exact_names_without_location_remain_review_candidates(sample, places):
    entity = incoming(sample, address=None, latitude=None, longitude=None)
    assert find_candidates(entity, places)[0]["score"] == 0.90


def test_candidate_sorting_and_limit_are_deterministic(sample, places):
    for entity_id in ("candidate-z", "candidate-a", "candidate-m"):
        places[entity_id] = places["sample-place"].model_copy(update={"id": entity_id})
    candidates = find_candidates(
        incoming(sample, name="Sample Bistr0"), places, DuplicatePolicy(max_candidates=2)
    )
    assert [c["id"] for c in candidates] == ["candidate-a", "candidate-m"]


def test_candidate_is_quarantined_and_never_overwrites_existing(repository, sample, places):
    original = deepcopy(repository.tables["restaurants"])
    payload = {
        "schema_version": "1.0",
        "request_id": "typo-candidate",
        "items": [incoming(sample, name="Sample Bistr0", local_name=None).model_dump(mode="json")],
    }
    service = IngestionService(repository)
    before = repository.read().digest
    report = service.ingest(payload)
    assert report["queued"] == 1 and report["created"] == 0
    assert report["results"][0]["duplicate_candidates"][0]["id"] == "sample-place"
    assert repository.read().digest == before
    report = service.ingest(payload, dry_run=False)
    assert report["results"][0]["reason"] == "duplicate_candidate"
    assert repository.tables["restaurants"] == original
    assert "duplicate_candidates" in repository.tables["verification_queue"][0]["errors_json"]
    assert service.ingest(payload, dry_run=False)["replayed"] is True


def test_fuzzy_duplicate_inside_single_batch_is_quarantined(repository, sample):
    first = sample["items"][-1]
    first.update(name="Sample Bistro", address="Test street 10")
    second = deepcopy(first)
    second.update(name="Sample Bistr0")
    sample["items"].append(second)
    report = IngestionService(repository).ingest(sample, dry_run=False)
    assert report["created"] == 3 and report["queued"] == 1
    assert len(repository.tables["restaurants"]) == 1


def test_region_alias_duplicate_has_evidence(sample, places):
    old = places["sample-city"]
    places[old.id] = old.model_copy(update={"aliases": ["Alias City"]})
    new = old.model_copy(update={"id": "new-city", "name": "Alias City", "local_name": None})
    candidates = find_candidates(new, places)
    assert candidates[0]["id"] == "sample-city"
    assert any(e["signal"] == "registered_alias" for e in candidates[0]["evidence"])


def test_same_country_code_is_a_candidate_even_when_names_are_unrelated(places):
    country = places["sample-country"].model_copy(
        update={"id": "new-country", "name": "Another language name", "local_name": None}
    )
    candidate = find_candidates(country, places)[0]
    assert candidate["id"] == "sample-country" and candidate["score"] == 0.99
    assert any(e["signal"] == "country_code_exact" for e in candidate["evidence"])


def test_transport_routes_with_known_distinct_endpoints_are_not_duplicates(sample, places):
    places["other-city"] = places["sample-city"].model_copy(update={"id": "other-city"})
    item = deepcopy(sample["items"][-1])
    item.update(
        kind="transport", transport_type="route", id="old-route", origin_region_id="sample-city"
    )
    old = ENTITY_ADAPTER.validate_python(item)
    places[old.id] = old
    new = old.model_copy(update={"id": "new-route", "origin_region_id": "other-city"})
    assert find_candidates(new, places) == []


def test_punctuation_only_names_do_not_match(sample, places):
    places["sample-place"] = places["sample-place"].model_copy(
        update={"name": "---", "local_name": None}
    )
    assert not find_candidates(incoming(sample, name="+++", local_name=None), places)


def test_zero_coordinates_are_actual_values(sample, places):
    old = places["sample-place"].model_copy(update={"latitude": 0.0, "longitude": 0.0})
    new = incoming(sample, latitude=0.0, longitude=0.0)
    assert distance_meters(new, old) == 0
