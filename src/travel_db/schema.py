"""One source of truth for tab names and machine-readable flat headers."""

import json

from .models import MODEL_BY_KIND, Common

TABLE_BY_KIND = {
    "region": "regions",
    "food": "foods",
    "restaurant": "restaurants",
    "experience": "experiences",
    "transport": "transport",
    "budget": "budgets",
    "rating": "ratings",
}
COMMON_COLUMNS = [
    "id",
    "kind",
    "category",
    "region_id",
    "name",
    "local_name",
    "source_url",
    "sources",
    "researched_on",
    "last_verified_on",
    "verification_status",
    "created_at",
    "updated_at",
]
HEADERS = {
    TABLE_BY_KIND[kind]: COMMON_COLUMNS
    + [f for f in model.model_fields if f not in Common.model_fields and f != "kind"]
    for kind, model in MODEL_BY_KIND.items()
}
HEADERS.update(
    {
        "sources": ["id", "url", "title", "publisher", "accessed_on"],
        "verification_queue": [
            "id",
            "request_id",
            "item_index",
            "reason",
            "target_id",
            "payload_json",
            "errors_json",
            "created_at",
        ],
        "change_history": [
            "id",
            "request_id",
            "entity_id",
            "table",
            "action",
            "before_json",
            "after_json",
            "created_at",
        ],
        "ingestion_runs": ["id", "payload_hash", "report_json", "created_at"],
        "inbox": ["request_id", "payload_json", "submitted_at"],
        "review_decisions": [
            "id",
            "queue_id",
            "decision",
            "entity_id",
            "reviewer",
            "reason",
            "request_id",
            "created_at",
        ],
        "field_sources": [
            "id",
            "entity_id",
            "entity_revision",
            "field",
            "source_ids",
            "basis",
            "verified_on",
            "request_id",
            "created_at",
        ],
    }
)
SCHEMA_VERSION = "1.1"


def dumps(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def cell_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool)):
        return dumps(value)
    return str(value)


def row_values(table: str, row: dict) -> list[str]:
    unknown = row.keys() - set(HEADERS[table])
    if unknown:
        raise ValueError(f"{table}: 알 수 없는 컬럼 {sorted(unknown)}")
    return [cell_value(row.get(key)) for key in HEADERS[table]]


def column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def initialization_requests(metadata: dict) -> list[dict]:
    """Add only missing managed tabs; never delete or rewrite any existing tab."""
    existing = {s["properties"]["title"] for s in metadata.get("sheets", [])}
    used_ids = {s["properties"]["sheetId"] for s in metadata.get("sheets", [])}
    requests = []
    next_id = max(used_ids, default=0) + 1
    for title, headers in HEADERS.items():
        if title in existing:
            continue
        sheet_id = next_id
        next_id += 1
        requests.extend(
            [
                {
                    "addSheet": {
                        "properties": {
                            "sheetId": sheet_id,
                            "title": title,
                            "gridProperties": {
                                "rowCount": 1000,
                                "columnCount": len(headers),
                                "frozenRowCount": 1,
                            },
                        }
                    }
                },
                {
                    "updateCells": {
                        "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                        "rows": [
                            {"values": [{"userEnteredValue": {"stringValue": h}} for h in headers]}
                        ],
                        "fields": "userEnteredValue",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "endColumnIndex": len(headers),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                                "textFormat": {"bold": True},
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": "userEnteredFormat",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": len(headers),
                        },
                        "properties": {"pixelSize": 180},
                        "fields": "pixelSize",
                    }
                },
                {
                    "setBasicFilter": {
                        "filter": {"range": {"sheetId": sheet_id, "endColumnIndex": len(headers)}}
                    }
                },
            ]
        )
    return requests
