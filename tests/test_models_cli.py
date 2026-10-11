from copy import deepcopy
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from travel_db.cli import main, parse_json, writer_lock
from travel_db.models import ENTITY_ADAPTER, ValidatedBatch
from travel_db.repository import DatabaseError, SheetsRepository


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", {"amount": "1000", "basis": "meal"}),
        ("price", {"amount": "-1", "currency": "JPY", "basis": "meal"}),
        ("latitude", 91),
        ("longitude", 181),
        ("latitude", 35),
        ("name", ""),
        ("unexpected_field", "unknown"),
        ("sources", []),
        ("last_verified_on", None),
        ("researched_on", "9999-12-31"),
        ("region_id", None),
    ],
)
def test_invalid_facts_fail_model_validation(sample, field, value):
    item = deepcopy(sample["items"][-1])
    item[field] = value
    with pytest.raises(ValidationError):
        ENTITY_ADAPTER.validate_python(item)


def test_unknown_price_and_coordinates_are_null(sample):
    item = sample["items"][-1]
    item["price"] = None
    validated = ENTITY_ADAPTER.validate_python(item)
    assert validated.price is None and validated.latitude is None


def test_future_source_date_is_rejected(sample):
    sample["items"][-1]["sources"][0]["accessed_on"] = (
        date.today() + timedelta(days=1)
    ).isoformat()
    with pytest.raises(ValidationError):
        ValidatedBatch.model_validate(sample)


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_ambiguous_or_nonstandard_json_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_json(payload)


def test_sample_validates_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("TRAVEL_SPREADSHEET_ID", raising=False)
    assert main(["validate", "examples/sample.json"]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_missing_secrets_gives_actionable_error(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("TRAVEL_SPREADSHEET_ID", raising=False)
    assert main(["init"]) == 2
    assert "Codespaces Secrets" in capsys.readouterr().out


def test_invalid_secret_is_not_echoed(monkeypatch):
    secret = "secret-content-must-not-be-logged"
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", secret)
    monkeypatch.setenv("TRAVEL_SPREADSHEET_ID", "sample-sheet")
    with pytest.raises(DatabaseError) as error:
        SheetsRepository.from_env()
    assert secret not in str(error.value)


def test_same_machine_writer_lock_blocks_overlapping_execution():
    with writer_lock("unit-test-sheet"):
        with pytest.raises(DatabaseError, match="실행 중"):
            with writer_lock("unit-test-sheet"):
                pytest.fail("second writer should never run")
