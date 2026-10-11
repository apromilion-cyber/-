"""Validated input contract. Unknown facts remain null; timestamps are server generated."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

import pycountry
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    create_model,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Price(StrictModel):
    amount: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
    currency: str
    basis: Text  # e.g. per meal, person, night; never infer units

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.upper()
        if pycountry.currencies.get(alpha_3=value) is None or value == "XXX":
            raise ValueError("ISO 4217 통화 코드가 필요합니다")
        return value


class Evidence(StrictModel):
    url: HttpUrl
    title: Text | None = None
    publisher: Text | None = None
    accessed_on: date

    @field_validator("accessed_on")
    @classmethod
    def not_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("출처 열람일은 미래일 수 없습니다")
        return value


class Common(StrictModel):
    id: Identifier | None = None
    region_id: Identifier | None
    name: Text
    local_name: Text | None
    sources: Annotated[list[Evidence], Field(max_length=30)]
    researched_on: date
    last_verified_on: date | None
    verification_status: Literal["verified", "unverified", "disputed"]

    @model_validator(mode="after")
    def check_evidence(self):
        if self.researched_on > date.today():
            raise ValueError("조사일은 미래일 수 없습니다")
        if self.last_verified_on is not None:
            if not self.researched_on <= self.last_verified_on <= date.today():
                raise ValueError("최종 확인일은 조사일 이후 및 오늘 이전이어야 합니다")
        if self.verification_status == "verified":
            if self.last_verified_on is None:
                raise ValueError("verified 항목은 최종 확인일이 필요합니다")
            if getattr(self, "kind", None) != "rating" and not self.sources:
                raise ValueError("verified 항목은 출처가 필요합니다")
        if self.region_id is None and getattr(self, "kind", None) != "region":
            raise ValueError("지역 ID가 필요합니다")
        return self


class Region(Common):
    kind: Literal["region"]
    level: Literal["country", "macro_region", "administrative_area", "city", "neighborhood"]
    parent_region_id: Identifier | None
    country_code: str
    aliases: list[Text] = Field(default_factory=list)

    @field_validator("country_code")
    @classmethod
    def valid_country(cls, value: str) -> str:
        value = value.upper()
        if pycountry.countries.get(alpha_2=value) is None:
            raise ValueError("ISO 3166-1 국가 코드가 필요합니다")
        return value

    @model_validator(mode="after")
    def valid_parent(self):
        if self.region_id != self.parent_region_id:
            raise ValueError("지역 항목의 region_id와 parent_region_id는 같아야 합니다")
        if (self.level == "country") != (self.parent_region_id is None):
            raise ValueError("국가만 부모 지역 없이 저장할 수 있습니다")
        if self.id and self.id == self.parent_region_id:
            raise ValueError("지역은 자신을 부모로 참조할 수 없습니다")
        return self


class Food(Common):
    kind: Literal["food"]
    food_type: Text
    ingredients: list[Text] = Field(default_factory=list)
    features: list[Text] = Field(default_factory=list)
    representative_region_ids: list[Identifier] = Field(default_factory=list)


class Place(Common):
    address: Text | None = None
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    price: Price | None = None

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("위도와 경도는 함께 입력해야 합니다")
        return self


class Restaurant(Place):
    kind: Literal["restaurant"]
    signature_food_ids: list[Identifier] = Field(default_factory=list)
    signature_menus: list[Text] = Field(default_factory=list)


class Experience(Place):
    kind: Literal["experience"]
    experience_type: Literal["attraction", "cafe", "activity", "shopping", "nature"]


class Transport(Place):
    kind: Literal["transport"]
    transport_type: Literal["airport", "train_station", "subway_station", "route"]
    origin_region_id: Identifier | None = None
    destination_region_id: Identifier | None = None
    accessibility: Text | None = None


class Budget(Common):
    kind: Literal["budget"]
    budget_type: Literal["lodging", "transport", "food", "trip_total"]
    price: Price | None = None
    duration_days: Annotated[int, Field(ge=1, le=3650)] | None = None
    people: Annotated[int, Field(ge=1, le=10000)] | None = None


class Rating(Common):
    kind: Literal["rating"]
    target_id: Identifier
    taste_fit: Annotated[int, Field(ge=1, le=5)] | None = None
    visited: bool
    satisfaction: Annotated[int, Field(ge=1, le=5)] | None = None
    notes: Text | None = None

    @model_validator(mode="after")
    def satisfaction_requires_visit(self):
        if self.satisfaction is not None and not self.visited:
            raise ValueError("방문하지 않은 항목에 만족도를 입력할 수 없습니다")
        return self


Entity = Annotated[
    Region | Food | Restaurant | Experience | Transport | Budget | Rating,
    Field(discriminator="kind"),
]
ENTITY_ADAPTER = TypeAdapter(Entity)
MODEL_BY_KIND = {
    "region": Region,
    "food": Food,
    "restaurant": Restaurant,
    "experience": Experience,
    "transport": Transport,
    "budget": Budget,
    "rating": Rating,
}


class Batch(StrictModel):
    schema_version: Literal["1.0", "1.1"]
    request_id: Identifier
    items: Annotated[list[dict], Field(min_length=1, max_length=500)]


class ValidatedBatch(StrictModel):
    """Public schema uses strict items; Batch permits individual failures to be quarantined."""

    schema_version: Literal["1.0"]
    request_id: Identifier
    items: Annotated[list[Entity], Field(min_length=1, max_length=500)]


# Input-only superset; classify first, then the strict Entity union rejects fields
# belonging to another kind. Stored models/Sheets headers and existing IDs stay stable.
_research_fields = {}
for _model in MODEL_BY_KIND.values():
    for _field, _info in _model.model_fields.items():
        if _field not in Common.model_fields and _info.is_required():
            _research_fields[_field] = (_info.annotation | None, None)
        else:
            _research_fields[_field] = (_info.annotation, deepcopy(_info))
for _field in (
    "kind",
    "category",
    "country",
    "city",
    "region",
    "level",
    "food_type",
    "experience_type",
    "transport_type",
    "budget_type",
    "country_code",
):
    _research_fields[_field] = (Text | None, None)
for _field in ("region_id", "parent_region_id"):
    _research_fields[_field] = (Identifier | None, None)
ResearchItem = create_model("ResearchItem", __base__=StrictModel, **_research_fields)


class ResearchBatch(StrictModel):
    schema_version: Literal["1.1"]
    request_id: Identifier
    items: Annotated[list[ResearchItem], Field(min_length=1, max_length=500)]


INPUT_ADAPTER = TypeAdapter(
    Annotated[ValidatedBatch | ResearchBatch, Field(discriminator="schema_version")]
)
