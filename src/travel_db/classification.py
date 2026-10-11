"""Deterministic classification using registered regions and a finite taxonomy."""

import unicodedata

from .models import ResearchItem


def label(value: str | None) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value or "").casefold() if c.isalnum())


class ClassificationError(ValueError):
    def __init__(self, code: str, message: str, candidates=()):
        super().__init__(message)
        self.code = code
        self.candidates = sorted(candidates)

    def detail(self) -> dict:
        return {"code": self.code, "message": str(self), "candidate_ids": self.candidates}


# Policy vocabulary, not researched travel facts. Unknown labels never fall back
# to name/address guessing. Food types remain open strings when explicitly supplied.
_CATEGORIES = {}


def _register(kind, field, value, *aliases):
    for alias in aliases:
        key = label(alias)
        entry = (kind, field, value)
        if key in _CATEGORIES and _CATEGORIES[key] != entry:
            raise RuntimeError(f"분류 사전 충돌: {alias}")
        _CATEGORIES[key] = entry


for _kind, _aliases in {
    "region": ("region", "지역"),
    "food": ("food", "음식"),
    "restaurant": ("restaurant", "restaurants", "음식점", "식당", "맛집", "レストラン"),
    "experience": ("experience", "체험", "관광체험"),
    "transport": ("transport", "교통"),
    "budget": ("budget", "예산"),
    "rating": ("rating", "평가", "개인평가"),
}.items():
    _register(_kind, None, None, *_aliases)
for _value, _aliases in {
    "country": ("국가", "country"),
    "macro_region": ("광역지역", "macro_region"),
    "administrative_area": ("행정구역", "administrative_area"),
    "city": ("도시", "city"),
    "neighborhood": ("세부지역", "neighborhood"),
}.items():
    _register("region", "level", _value, *_aliases)
for _value, _aliases in {
    "attraction": ("관광지", "attraction"),
    "cafe": ("카페", "cafe", "café", "カフェ"),
    "activity": ("액티비티", "activity"),
    "shopping": ("쇼핑", "shopping"),
    "nature": ("자연명소", "nature"),
}.items():
    _register("experience", "experience_type", _value, *_aliases)
for _value, _aliases in {
    "airport": ("공항", "airport"),
    "train_station": ("기차역", "철도역", "train_station"),
    "subway_station": ("지하철역", "subway_station"),
    "route": ("교통편", "노선", "route"),
}.items():
    _register("transport", "transport_type", _value, *_aliases)
for _value, _aliases in {
    "lodging": ("숙박비", "lodging"),
    "transport": ("교통비", "transport_cost"),
    "food": ("식비", "food_cost"),
    "trip_total": ("여행총비용", "trip_total"),
}.items():
    _register("budget", "budget_type", _value, *_aliases)
for _value, _aliases in {
    "ramen": ("라멘", "ramen", "ラーメン"),
    "noodles": ("면요리", "noodles"),
    "rice": ("밥요리", "rice"),
    "soup": ("국물요리", "soup"),
    "seafood": ("해산물요리", "seafood"),
    "meat": ("고기요리", "meat"),
    "dessert": ("디저트", "dessert"),
    "beverage": ("음료", "beverage"),
    "street_food": ("길거리음식", "street_food"),
}.items():
    _register("food", "food_type", _value, *_aliases)

SUBTYPE_FIELDS = {
    "region": "level",
    "food": "food_type",
    "experience": "experience_type",
    "transport": "transport_type",
    "budget": "budget_type",
}


def category_rule(value: str) -> tuple:
    rule = _CATEGORIES.get(label(value))
    if rule is None:
        raise ClassificationError("unknown_category", "등록되지 않은 category입니다")
    return rule


def resolve_scope(hints: dict, entities: dict) -> dict:
    # Reuse region-hint consistency checks without inventing a travel entity or facts.
    _, classification = resolve_item({"kind": "restaurant", **hints}, entities, [])
    return {k: classification[k] for k in ("region_id", "country_id", "city_id", "rules")}


def prepare_item(raw: dict) -> tuple[dict, list[dict]]:
    """Validate input shape and normalize classification, without needing a database."""
    data = ResearchItem.model_validate(raw).model_dump(mode="json", exclude_unset=True)
    rules = []
    supplied_kind = data.get("kind")
    explicit = _CATEGORIES.get(label(supplied_kind)) if supplied_kind else None
    if supplied_kind and explicit is None:
        raise ClassificationError("unknown_kind", "등록되지 않은 kind입니다")
    category = data.get("category")
    category_rule = _CATEGORIES.get(label(category)) if category else None
    if category and category_rule is None:
        raise ClassificationError("unknown_category", "등록되지 않은 category입니다")
    if explicit and category_rule and explicit[0] != category_rule[0]:
        raise ClassificationError("category_conflict", "kind와 category의 종류가 다릅니다")
    inferred = explicit or category_rule
    if inferred is None:
        clues = [kind for kind, field in SUBTYPE_FIELDS.items() if data.get(field)]
        if data.get("target_id"):
            clues.append("rating")
        if len(clues) != 1:
            raise ClassificationError("ambiguous_kind", "kind 또는 명확한 category가 필요합니다")
        inferred = (clues[0], None, None)
    kind = inferred[0]
    if supplied_kind != kind:
        rules.append(
            {
                "field": "kind",
                "input": supplied_kind,
                "resolved": kind,
                "rule": "category_dictionary" if explicit or category_rule else "subtype_field",
            }
        )
    data["kind"] = kind
    subtype = SUBTYPE_FIELDS.get(kind)
    if subtype and data.get(subtype):
        value = data[subtype]
        # Canonical subtype strings such as budget_type='food' are context specific.
        canonical_values = {
            label(entry[2]): entry[2]
            for entry in _CATEGORIES.values()
            if entry[0] == kind and entry[1] == subtype
        }
        entry = _CATEGORIES.get(label(value))
        if label(value) in canonical_values:
            normalized = canonical_values[label(value)]
        elif entry and entry[:2] == (kind, subtype):
            normalized = entry[2]
        elif kind == "food":
            normalized = value  # Existing open food taxonomy stays valid.
        else:
            raise ClassificationError("unknown_subtype", f"등록되지 않은 {subtype}입니다")
        if normalized != value:
            rules.append(
                {
                    "field": subtype,
                    "input": value,
                    "resolved": normalized,
                    "rule": "category_dictionary",
                }
            )
        data[subtype] = normalized
    for entry in (explicit, category_rule):
        if entry is None or entry[1] is None:
            continue
        _, field, value = entry
        if data.get(field) is not None and data[field] != value:
            raise ClassificationError("category_conflict", f"category와 {field}가 다릅니다")
        if data.get(field) is None:
            data[field] = value
            rules.append(
                {
                    "field": field,
                    "input": category or supplied_kind,
                    "resolved": value,
                    "rule": "category_dictionary",
                }
            )
    return data, rules


def ancestors(region_id: str | None, entities: dict) -> list:
    chain, seen = [], set()
    while region_id and region_id not in seen:
        region = entities.get(region_id)
        if region is None or region.kind != "region":
            break
        chain.append(region)
        seen.add(region_id)
        region_id = region.parent_region_id
    return chain


def region_names(region) -> set[str]:
    return {
        normalized
        for v in [region.id, region.name, region.local_name, *region.aliases]
        if (normalized := label(v))
    }


def _one(candidates, field):
    if not candidates:
        raise ClassificationError("region_not_found", f"등록된 {field} 지역을 찾을 수 없습니다")
    if len(candidates) > 1:
        raise ClassificationError(
            "ambiguous_region", f"{field} 지역 후보가 여러 개입니다", [r.id for r in candidates]
        )
    return candidates[0]


def resolve_item(prepared: dict, entities: dict, rules: list[dict]) -> tuple[dict, dict]:
    data, rules = dict(prepared), list(rules)
    regions = [e for e in entities.values() if e.kind == "region"]
    country_hint, city_hint, region_hint = (
        data.pop(k, None) for k in ("country", "city", "region")
    )
    data.pop("category", None)
    is_region = data["kind"] == "region"
    is_country = is_region and data.get("level") == "country"
    explicit_id = data.get("region_id") or (data.get("parent_region_id") if is_region else None)
    selected = entities.get(explicit_id) if explicit_id else None
    if explicit_id and (selected is None or selected.kind != "region"):
        raise ClassificationError("region_not_found", "명시한 지역 ID를 찾을 수 없습니다")
    chain = ancestors(explicit_id, entities)
    country = next((r for r in chain if r.level == "country"), None)
    city = next((r for r in chain if r.level == "city"), None)
    if country_hint:
        matches = [
            r
            for r in regions
            if r.level == "country"
            and (
                label(country_hint) in region_names(r)
                or label(country_hint) == label(r.country_code)
            )
        ]
        if country:
            if country.id not in {r.id for r in matches}:
                raise ClassificationError("region_conflict", "국가 힌트와 지역 ID가 다릅니다")
            matches = [country]
        hinted = _one(matches, "country")
        if country and country.id != hinted.id:
            raise ClassificationError("region_conflict", "국가 힌트와 지역 ID가 다릅니다")
        country = hinted
    if city_hint:
        matches = [
            r
            for r in regions
            if r.level == "city"
            and label(city_hint) in region_names(r)
            and (country is None or r.country_code == country.country_code)
            and (city is None or r.id == city.id)
        ]
        hinted = _one(matches, "city")
        if selected and hinted.id not in {r.id for r in chain}:
            raise ClassificationError("region_conflict", "도시 힌트와 지역 ID가 다릅니다")
        city = hinted
    if region_hint:
        matches = [
            r
            for r in regions
            if label(region_hint) in region_names(r)
            and (country is None or r.country_code == country.country_code)
            and (city is None or city.id in {a.id for a in ancestors(r.id, entities)})
        ]
        hinted = _one(matches, "region")
        if selected and selected.id != hinted.id:
            raise ClassificationError("region_conflict", "지역 힌트와 지역 ID가 다릅니다")
        selected = hinted
    selected = selected or city or country
    if is_country:
        if explicit_id or country_hint or city_hint or region_hint:
            raise ClassificationError(
                "region_conflict", "새 국가 항목은 부모/지역 힌트를 갖지 않습니다"
            )
        data.setdefault("region_id", None)
        data.setdefault("parent_region_id", None)
    else:
        if selected is None:
            raise ClassificationError(
                "region_not_found", "지역 ID 또는 등록된 지역 힌트가 필요합니다"
            )
        if data.get("region_id") != selected.id:
            rules.append(
                {
                    "field": "region_id",
                    "input": data.get("region_id"),
                    "resolved": selected.id,
                    "rule": "registered_region_alias",
                }
            )
        data["region_id"] = selected.id
        if is_region:
            if not data.get("parent_region_id"):
                data["parent_region_id"] = selected.id
            if not data.get("country_code"):
                data["country_code"] = selected.country_code
    chain = ancestors(selected.id if selected else None, entities)
    country = next((r for r in chain if r.level == "country"), None)
    city = next((r for r in chain if r.level == "city"), None)
    return data, {
        "kind": data["kind"],
        "region_id": data.get("region_id"),
        "country_id": country.id if country else None,
        "city_id": city.id if city else None,
        "rules": rules,
    }
