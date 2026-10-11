"""Explicit, reviewable stage-3 commands. Credentials are never part of this contract."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, TypeAdapter, model_validator

from .models import Entity, Evidence, Identifier, StrictModel, Text

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
FieldSources = dict[Text, Annotated[list[HttpUrl], Field(max_length=30)]]


class FieldProof(StrictModel):
    source_ids: Annotated[list[Identifier], Field(max_length=30)]
    basis: Literal["sources", "personal"]
    verified_on: date

    @model_validator(mode="after")
    def valid_date(self):
        Evidence.not_future(self.verified_on)
        return self


class Operation(StrictModel):
    schema_version: Literal["1.0"]
    request_id: Identifier
    reviewer: Text
    reason: Text


class Approve(Operation):
    action: Literal["approve"]
    queue_id: Identifier
    mode: Literal["create", "update"]
    entity: Entity
    target_id: Identifier | None = None
    expected_hash: Hash | None = None
    field_sources: FieldSources = Field(default_factory=dict)
    distinct_from: Annotated[list[Identifier], Field(max_length=500)] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_mode(self):
        if self.mode == "update":
            if self.target_id is None or self.expected_hash is None:
                raise ValueError("update 승인에는 target_id와 expected_hash가 필요합니다")
        elif self.target_id is not None or self.expected_hash is not None:
            raise ValueError("create 승인에는 업데이트 대상을 지정할 수 없습니다")
        return self


class Reject(Operation):
    action: Literal["reject"]
    queue_id: Identifier


class Update(Operation):
    action: Literal["update"]
    target_id: Identifier
    expected_hash: Hash
    entity: Entity
    field_sources: FieldSources = Field(default_factory=dict)
    distinct_from: Annotated[list[Identifier], Field(max_length=500)] = Field(default_factory=list)


class Restore(Operation):
    action: Literal["restore"]
    history_id: Identifier
    expected_hash: Hash


OPERATION_ADAPTER = TypeAdapter(
    Annotated[Approve | Reject | Update | Restore, Field(discriminator="action")]
)
