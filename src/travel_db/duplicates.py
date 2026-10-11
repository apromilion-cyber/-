"""Explainable review candidates; never merge by a heuristic score."""

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .classification import ancestors, label, region_names


@dataclass(frozen=True)
class DuplicatePolicy:
    strong_name: float = 0.90
    minimum_name: float = 0.75
    name_only: float = 0.94
    nearby_meters: float = 100
    distant_branch_meters: float = 300
    candidate_threshold: float = 0.85
    max_candidates: int = 5


def distance_meters(a, b) -> float | None:
    values = [getattr(e, field, None) for e in (a, b) for field in ("latitude", "longitude")]
    if any(v is None for v in values):
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, values)
    term = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1, max(0, term))))


def same_scope(a, b, entities):
    if a.kind == "region":
        return (a.country_code, a.level, a.parent_region_id) == (
            b.country_code,
            b.level,
            b.parent_region_id,
        )
    if a.region_id == b.region_id:
        return True
    if a.kind not in {"restaurant", "experience", "transport"}:
        return False
    cities = [
        next((r.id for r in ancestors(e.region_id, entities) if r.level == "city"), None)
        for e in (a, b)
    ]
    return cities[0] is not None and cities[0] == cities[1]


def _name_match(a, b):
    pairs = [
        (SequenceMatcher(None, label(av), label(bv), autojunk=False).ratio(), af, bf)
        for af in ("name", "local_name")
        for bf in ("name", "local_name")
        if (av := getattr(a, af)) and (bv := getattr(b, bf)) and label(av) and label(bv)
    ]
    ratio, af, bf = max(pairs, default=(0.0, "name", "name"))
    return ratio, {
        "signal": "name_similarity",
        "similarity": round(ratio, 4),
        "input_field": af,
        "existing_field": bf,
    }


def find_candidates(entity, entities: dict, policy: DuplicatePolicy | None = None) -> list[dict]:
    policy = policy or DuplicatePolicy()
    if entity.kind in {"budget", "rating"}:
        return []  # Use their explicit logical keys, not text similarity.
    candidates = []
    for old in entities.values():
        if entity.kind != old.kind or not same_scope(entity, old, entities):
            continue
        if any(
            getattr(entity, f, None) != getattr(old, f, None)
            for f in ("experience_type", "transport_type")
        ):
            continue
        if entity.kind == "transport" and entity.transport_type == "route":
            if any(
                getattr(entity, f) and getattr(old, f) and getattr(entity, f) != getattr(old, f)
                for f in ("origin_region_id", "destination_region_id")
            ):
                continue
        similarity, name_evidence = _name_match(entity, old)
        evidence = [name_evidence]
        score = 0.0
        if entity.kind == "region":
            if entity.level == "country":
                evidence.append(
                    {"signal": "country_code_exact", "country_code": entity.country_code}
                )
                score = 0.99
            aliases = region_names(entity) & region_names(old)
            if aliases:
                evidence.append({"signal": "registered_alias", "matches": sorted(aliases)})
                score = 0.95
        address = label(getattr(entity, "address", None))
        old_address = label(getattr(old, "address", None))
        paired_addresses = bool(address and old_address)
        address_exact = paired_addresses and address == old_address
        address_similarity = (
            SequenceMatcher(None, address, old_address, autojunk=False).ratio()
            if paired_addresses
            else 0
        )
        address_numbers_equal = re.findall(r"\d+", address) == re.findall(r"\d+", old_address)
        address_close = paired_addresses and address_similarity >= 0.90 and address_numbers_equal
        distance = distance_meters(entity, old)
        near = distance is not None and distance <= policy.nearby_meters
        if address_exact:
            evidence.append({"signal": "address_exact"})
        elif paired_addresses:
            evidence.append(
                {
                    "signal": "address_similarity",
                    "similarity": round(address_similarity, 4),
                    "numbers_equal": address_numbers_equal,
                }
            )
        if distance is not None:
            evidence.append({"signal": "coordinate_distance", "meters": round(distance, 2)})
            if distance > policy.distant_branch_meters and not address_exact:
                continue  # Same-name chain branches with distinct physical locations.
        if similarity >= policy.strong_name and (address_exact or near):
            score = max(score, 0.97)
        elif similarity >= policy.minimum_name and address_exact:
            score = max(score, 0.93)
        elif similarity == 1 and (not paired_addresses or address_close):
            score = max(score, 0.90)
        elif similarity >= policy.name_only and not paired_addresses:
            score = max(score, 0.85)
        if score >= policy.candidate_threshold:
            candidates.append(
                {"id": old.id, "score": score, "policy_version": "1", "evidence": evidence}
            )
    return sorted(candidates, key=lambda c: (-c["score"], c["id"]))[: policy.max_candidates]
