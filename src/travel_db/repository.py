"""Google Sheets REST adapter. No local persistent database or automatic write retries."""

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from requests.exceptions import RequestException

from .schema import (
    HEADERS,
    TABLE_BY_KIND,
    cell_value,
    column_letter,
    dumps,
    initialization_requests,
    row_values,
)


class DatabaseError(Exception):
    pass


class ConflictError(DatabaseError):
    pass


@dataclass
class Snapshot:
    tables: dict[str, list[dict]]
    row_positions: dict[str, list[int]] = field(default_factory=dict)
    sheet_ids: dict[str, int] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            dumps(
                {
                    "tables": self.tables,
                    "positions": self.row_positions,
                    "sheet_ids": self.sheet_ids,
                }
            ).encode()
        ).hexdigest()


class Repository(Protocol):
    def read(self) -> Snapshot: ...
    def commit(
        self,
        additions: dict[str, list[dict]],
        expected: Snapshot,
        *,
        updates: dict[str, list[dict]] | None = None,
    ) -> None: ...


def planned_snapshot(snapshot: Snapshot, additions: dict, updates: dict | None = None) -> Snapshot:
    """Build exactly the intended state; operational tables remain append-only."""
    updates = updates or {}
    if (set(additions) | set(updates)) - set(HEADERS):
        raise DatabaseError("알 수 없는 변경 대상 탭입니다")
    tables = {}
    for table, rows in snapshot.tables.items():
        replacements = updates.get(table, [])
        if replacements and table not in TABLE_BY_KIND.values():
            raise DatabaseError("운영 기록은 수정할 수 없습니다")
        by_id = {}
        for row in replacements:
            if set(row) != set(HEADERS[table]) or row["id"] in by_id:
                raise DatabaseError("업데이트 컬럼/ID가 잘못되었습니다")
            by_id[row["id"]] = row
        current_ids = [r.get("id") for r in rows]
        if any(current_ids.count(key) != 1 for key in by_id):
            raise ConflictError("업데이트 대상 ID가 없거나 중복되었습니다")
        tables[table] = [
            {h: cell_value(v) for h, v in by_id.get(r.get("id"), r).items()} for r in rows
        ]
        for row in additions.get(table, []):
            row_values(table, row)
            tables[table].append({h: cell_value(row.get(h)) for h in HEADERS[table]})
        if any(len(v) > 45_000 for r in tables[table] for v in r.values()):
            raise DatabaseError("셀 내용은 45,000자 이하여야 합니다")
    # Bound serialized request size in dry-run too. Maximum integer coordinates
    # conservatively bound the actual IDs/positions without deriving row order.
    requests = []
    for table, rows in additions.items():
        if rows:
            requests.append(
                {
                    "appendCells": {
                        "sheetId": 2147483647,
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": v}}
                                    for v in row_values(table, row)
                                ]
                            }
                            for row in rows
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            )
    for table, rows in updates.items():
        for row in rows:
            requests.append(
                {
                    "updateCells": {
                        "start": {"sheetId": 2147483647, "rowIndex": 2147483647, "columnIndex": 0},
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": v}}
                                    for v in row_values(table, row)
                                ]
                            }
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            )
    if len(dumps({"requests": requests}).encode()) > 2_000_000:
        raise DatabaseError(
            "원자적 저장 요청은 2 MB 이하여야 합니다. 전체 복구를 부분 저장하지 않습니다"
        )
    return Snapshot(tables)


class SheetsRepository:
    def __init__(self, spreadsheet_id: str, session):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            raise DatabaseError("유효한 TRAVEL_SPREADSHEET_ID가 필요합니다")
        self.spreadsheet_id = spreadsheet_id
        self.session = session
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"

    @classmethod
    def from_env(cls):
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        spreadsheet_id = os.environ.get("TRAVEL_SPREADSHEET_ID")
        if not raw or not spreadsheet_id:
            raise DatabaseError(
                "Codespaces Secrets의 GOOGLE_SERVICE_ACCOUNT_JSON과 "
                "TRAVEL_SPREADSHEET_ID를 설정하세요"
            )
        try:
            info = json.loads(raw)
            if (
                not isinstance(info, dict)
                or info.get("token_uri") != "https://oauth2.googleapis.com/token"
            ):
                raise ValueError("invalid credentials")
            credentials = Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DatabaseError(
                "서비스 계정 Secret 형식이 잘못되었습니다 (내용은 출력하지 않음)"
            ) from exc
        # Disable AuthorizedSession's 401 refresh retry for writes; outcome may be uncertain.
        return cls(spreadsheet_id, AuthorizedSession(credentials, max_refresh_attempts=0))

    def _request(self, method: str, path: str = "", **kwargs):
        try:
            for attempt in range(7):
                response = self.session.request(method, self.base + path, timeout=30, **kwargs)
                if method != "GET" or response.status_code != 429 or attempt == 6:
                    break
                # Only safe reads retry quota errors. POST outcomes must never be retried.
                time.sleep(min(2**attempt, 32) + random.random())
        except (RequestException, GoogleAuthError) as exc:
            raise DatabaseError(
                "Google API 연결/인증 오류. 쓰기 중이었다면 같은 request_id로 영수증을 확인하세요"
            ) from exc
        if not response.ok:
            # Never print response bodies, which may echo private data or credentials.
            raise DatabaseError(
                f"Google Sheets API HTTP {response.status_code}; 권한/요청을 확인하세요"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise DatabaseError(
                "Google API 응답을 읽을 수 없습니다. 쓰기 결과를 재확인하세요"
            ) from exc

    def metadata(self) -> dict:
        return self._request(
            "GET", params={"fields": "spreadsheetId,properties.title,sheets.properties"}
        )

    @staticmethod
    def _range(title: str, start_row: int, end_row: int, width: int) -> str:
        safe_title = title.replace("'", "''")
        return f"'{safe_title}'!A{start_row}:{column_letter(width)}{end_row}"

    def _batch_values(self, ranges: list[str]) -> list[list[list]]:
        if not ranges:
            return []
        response = self._request(
            "GET",
            "/values:batchGet",
            params={"ranges": ranges, "valueRenderOption": "UNFORMATTED_VALUE"},
        )
        blocks = response.get("valueRanges", [])
        if len(blocks) != len(ranges):
            raise DatabaseError("Google Sheets 읽기 응답의 범위 수가 맞지 않습니다")
        return [block.get("values", []) for block in blocks]

    def _check_headers(self, metadata: dict, require_all: bool):
        properties = {s["properties"]["title"]: s["properties"] for s in metadata["sheets"]}
        titles = []
        ranges = []
        for title in HEADERS:
            if title not in properties:
                if require_all:
                    raise DatabaseError(f"{title} 탭이 없습니다. init --apply가 필요합니다")
                continue
            grid = properties[title]["gridProperties"]
            # Read the whole existing header to catch unexpected additional columns.
            titles.append(title)
            ranges.append(self._range(title, 1, 1, grid["columnCount"]))
        for title, values in zip(titles, self._batch_values(ranges), strict=True):
            if not values or values[0] != HEADERS[title]:
                raise DatabaseError(f"{title} 헤더가 스키마와 다릅니다. 자동 수정하지 않습니다")
        return properties

    def initialize(self, *, dry_run: bool = True) -> dict:
        metadata = self.metadata()
        self._check_headers(metadata, require_all=False)
        requests = initialization_requests(metadata)
        titles = [r["addSheet"]["properties"]["title"] for r in requests if "addSheet" in r]
        if requests and not dry_run:
            self._request("POST", ":batchUpdate", json={"requests": requests})
            self._check_headers(self.metadata(), require_all=True)
        return {"dry_run": dry_run, "created_tabs": titles}

    def read(self) -> Snapshot:
        properties = self._check_headers(self.metadata(), require_all=True)
        tables = {title: [] for title in HEADERS}
        positions = {title: [] for title in HEADERS}
        chunks = []
        for title, headers in HEADERS.items():
            grid_rows = properties[title]["gridProperties"]["rowCount"]
            page_size = min(1000, max(1, 40_000 // len(headers)))
            for start in range(2, grid_rows + 1, page_size):
                end = min(start + page_size - 1, grid_rows)
                chunks.append(
                    (
                        title,
                        self._range(title, start, end, len(headers)),
                        (end - start + 1) * len(headers),
                        start,
                    )
                )
        # Bundle bounded blocks to avoid exhausting the API read quota for small requests.
        groups = []
        group = []
        cells = 0
        for chunk in chunks:
            if group and cells + chunk[2] > 200_000:
                groups.append(group)
                group, cells = [], 0
            group.append(chunk)
            cells += chunk[2]
        if group:
            groups.append(group)
        for group in groups:
            blocks = self._batch_values([c[1] for c in group])
            for (title, _, _, start), values in zip(group, blocks, strict=True):
                headers = HEADERS[title]
                for offset, row in enumerate(values):
                    if not any(value != "" for value in row):
                        continue
                    if len(row) > len(headers):
                        raise DatabaseError(f"{title}에 스키마 밖 값이 있습니다")
                    tables[title].append(
                        {
                            h: cell_value(row[i]) if i < len(row) else ""
                            for i, h in enumerate(headers)
                        }
                    )
                    positions[title].append(start - 1 + offset)
        return Snapshot(
            tables, positions, {t: p["sheetId"] for t, p in properties.items() if t in HEADERS}
        )

    def read_page(self, table: str, *, start_row: int = 2, page_size: int = 200) -> dict:
        if table not in HEADERS or start_row < 2 or not 1 <= page_size <= 1000:
            raise ValueError("올바른 탭과 start_row>=2, page_size=1~1000이 필요합니다")
        properties = self._check_headers(self.metadata(), require_all=True)
        grid_rows = properties[table]["gridProperties"]["rowCount"]
        end = min(start_row + page_size - 1, grid_rows)
        if start_row > grid_rows:
            values = []
        else:
            values = self._batch_values([self._range(table, start_row, end, len(HEADERS[table]))])[
                0
            ]
        rows = []
        for offset, values_row in enumerate(values):
            if not any(value != "" for value in values_row):
                continue
            if len(values_row) > len(HEADERS[table]):
                raise DatabaseError("페이지에 스키마 밖 값이 있습니다")
            rows.append(
                {
                    "row_number": start_row + offset,
                    "row": {
                        h: cell_value(values_row[i]) if i < len(values_row) else ""
                        for i, h in enumerate(HEADERS[table])
                    },
                }
            )
        return {
            "table": table,
            "sheet_id": properties[table]["sheetId"],
            "start_row": start_row,
            "end_row": end if start_row <= grid_rows else None,
            "grid_rows": grid_rows,
            "next_row": end + 1 if end < grid_rows else None,
            "rows": rows,
            "validation_mode": "headers_only",
        }

    def commit(
        self,
        additions: dict[str, list[dict]],
        expected: Snapshot,
        *,
        updates: dict[str, list[dict]] | None = None,
    ) -> None:
        # A best-effort conflict check, not a Sheets compare-and-swap transaction.
        current = self.read()
        if current.digest != expected.digest:
            raise ConflictError("계획 이후 DB가 변경되었습니다. 다시 dry-run을 실행하세요")
        planned_snapshot(current, additions, updates)
        ids = current.sheet_ids
        requests = []
        for table, rows in (updates or {}).items():
            locations = {
                r["id"]: position
                for r, position in zip(
                    current.tables[table], current.row_positions[table], strict=True
                )
            }
            for row in rows:
                requests.append(
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": ids[table],
                                "rowIndex": locations[row["id"]],
                                "columnIndex": 0,
                            },
                            "rows": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"stringValue": v}}
                                        for v in row_values(table, row)
                                    ]
                                }
                            ],
                            "fields": "userEnteredValue",
                        }
                    }
                )
        for table, rows in additions.items():
            if not rows:
                continue
            requests.append(
                {
                    "appendCells": {
                        "sheetId": ids[table],
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": value}}
                                    for value in row_values(table, row)
                                ]
                            }
                            for row in rows
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            )
        if requests:
            body = {"requests": requests}
            if len(dumps(body).encode()) > 2_000_000:
                raise DatabaseError("한 번에 저장할 데이터가 큽니다. 요청을 작은 배치로 나누세요")
            self._request("POST", ":batchUpdate", json=body)
