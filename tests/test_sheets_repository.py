from copy import deepcopy

import pytest
from requests.exceptions import Timeout

from travel_db.repository import ConflictError, DatabaseError, SheetsRepository
from travel_db.schema import HEADERS, initialization_requests


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return deepcopy(self.body)


class Session:
    def __init__(self):
        self.calls = []
        self.headers = deepcopy(HEADERS)
        self.data = {title: [] for title in HEADERS}
        self.write_error = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST":
            if self.write_error:
                raise self.write_error
            return Response({"replies": []})
        if url.endswith("/values:batchGet"):
            blocks = []
            for a1 in kwargs["params"]["ranges"]:
                title, coords = a1.split("!")
                title = title.strip("'")
                values = [self.headers[title]] if coords.startswith("A1:") else self.data[title]
                blocks.append({"range": a1, "values": values})
            return Response({"valueRanges": blocks})
        return Response(
            {
                "sheets": [
                    {
                        "properties": {
                            "title": title,
                            "sheetId": i,
                            "gridProperties": {"rowCount": 3, "columnCount": len(h)},
                        }
                    }
                    for i, (title, h) in enumerate(self.headers.items())
                ]
            }
        )


def test_commit_is_one_atomic_request_with_literal_strings():
    session = Session()
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    repository.commit(
        {
            "inbox": [
                {
                    "request_id": "test-request",
                    "payload_json": "=IMPORTXML(test)",
                    "submitted_at": "2026-01-01",
                }
            ],
            "ingestion_runs": [
                {
                    "id": "test-request",
                    "payload_hash": "hash",
                    "report_json": "{}",
                    "created_at": "2026-01-01",
                }
            ],
        },
        snapshot,
    )
    writes = [c for c in session.calls if c[0] == "POST"]
    assert len(writes) == 1
    requests = writes[0][2]["json"]["requests"]
    assert len(requests) == 2
    payload = requests[0]["appendCells"]["rows"][0]["values"][1]
    assert payload == {"userEnteredValue": {"stringValue": "=IMPORTXML(test)"}}


def test_changed_snapshot_aborts_before_post():
    session = Session()
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    session.data["inbox"].append(["other", "{}", "date"])
    with pytest.raises(ConflictError):
        repository.commit({"inbox": []}, snapshot)
    assert not any(c[0] == "POST" for c in session.calls)


def test_wrong_header_aborts_and_never_repairs_existing_data():
    session = Session()
    session.headers["restaurants"][0] = "unexpected-header"
    repository = SheetsRepository("test-sheet", session)
    with pytest.raises(DatabaseError, match="헤더"):
        repository.initialize(dry_run=False)
    assert not any(c[0] == "POST" for c in session.calls)


def test_empty_init_plan_does_not_write():
    session = Session()
    report = SheetsRepository("test-sheet", session).initialize(dry_run=False)
    assert report["created_tabs"] == []
    assert not any(c[0] == "POST" for c in session.calls)


def test_snapshot_batches_reads_and_uses_finite_ranges():
    session = Session()
    SheetsRepository("test-sheet", session).read()
    assert len(session.calls) == 3  # metadata, all headers, all small test data blocks
    ranges = [
        r
        for c in session.calls
        if c[1].endswith("/values:batchGet")
        for r in c[2]["params"]["ranges"]
    ]
    assert len(ranges) == 2 * len(HEADERS)
    assert all("A1:" in r or "A2:" in r for r in ranges)


def test_init_only_adds_missing_tabs():
    requests = initialization_requests(
        {"sheets": [{"properties": {"title": "regions", "sheetId": 7}}]}
    )
    titles = [r["addSheet"]["properties"]["title"] for r in requests if "addSheet" in r]
    assert "regions" not in titles
    assert set(titles) == set(HEADERS) - {"regions"}
    assert not any("deleteSheet" in r for r in requests)


def test_timed_out_write_is_not_automatically_retried():
    session = Session()
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    session.write_error = Timeout("sensitive fake body")
    with pytest.raises(DatabaseError) as error:
        repository.commit(
            {"inbox": [{"request_id": "r", "payload_json": "{}", "submitted_at": ""}]}, snapshot
        )
    assert "sensitive fake body" not in str(error.value)
    assert len([c for c in session.calls if c[0] == "POST"]) == 1


def test_update_targets_physical_row_after_blank_and_is_atomic_with_audit():
    session = Session()
    row = {h: "" for h in HEADERS["restaurants"]}
    row.update(id="sample-target", name="before")
    session.data["restaurants"] = [[], [row[h] for h in HEADERS["restaurants"]]]
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    assert snapshot.row_positions["restaurants"] == [2]
    changed = {**row, "name": "=FAKE_FORMULA()"}
    repository.commit(
        {"change_history": [{"id": "history"}], "ingestion_runs": [{"id": "request"}]},
        snapshot,
        updates={"restaurants": [changed]},
    )
    writes = [c for c in session.calls if c[0] == "POST"]
    assert len(writes) == 1
    requests = writes[0][2]["json"]["requests"]
    assert len(requests) == 3
    update = requests[0]["updateCells"]
    assert update["start"]["rowIndex"] == 2
    assert update["start"]["columnIndex"] == 0
    assert update["fields"] == "userEnteredValue"
    name_cell = update["rows"][0]["values"][HEADERS["restaurants"].index("name")]
    assert name_cell == {"userEnteredValue": {"stringValue": "=FAKE_FORMULA()"}}


def test_moving_row_without_value_change_is_a_conflict():
    session = Session()
    values = ["target"] + [""] * (len(HEADERS["restaurants"]) - 1)
    session.data["restaurants"] = [values]
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    session.data["restaurants"] = [[], values]
    with pytest.raises(ConflictError):
        repository.commit({}, snapshot, updates={"restaurants": snapshot.tables["restaurants"]})
    assert not any(c[0] == "POST" for c in session.calls)


@pytest.mark.parametrize(
    "table", ["ingestion_runs", "verification_queue", "review_decisions", "field_sources"]
)
def test_operational_records_cannot_be_overwritten(table):
    session = Session()
    session.data[table] = [["target"]]
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    with pytest.raises(DatabaseError, match="운영 기록"):
        repository.commit({}, snapshot, updates={table: snapshot.tables[table]})
    assert not any(c[0] == "POST" for c in session.calls)


def test_update_timeout_is_one_attempt_including_all_audit_rows():
    session = Session()
    session.data["restaurants"] = [["target"]]
    repository = SheetsRepository("test-sheet", session)
    snapshot = repository.read()
    session.write_error = Timeout("private response")
    with pytest.raises(DatabaseError):
        repository.commit(
            {"change_history": [{"id": "h"}]},
            snapshot,
            updates={"restaurants": snapshot.tables["restaurants"]},
        )
    assert len([c for c in session.calls if c[0] == "POST"]) == 1


def test_physical_positions_are_correct_across_pages():
    class PagedSession(Session):
        def request(self, method, url, **kwargs):
            if url.endswith("/values:batchGet"):
                self.calls.append((method, url, kwargs))
                blocks = []
                for a1 in kwargs["params"]["ranges"]:
                    title, coords = a1.split("!")
                    title = title.strip("'")
                    if coords.startswith("A1:"):
                        values = [self.headers[title]]
                    else:
                        import re

                        start, end = map(int, re.findall(r"\d+", coords))
                        values = self.data[title][start - 2 : end - 1]
                    blocks.append({"values": values})
                return Response({"valueRanges": blocks})
            result = super().request(method, url, **kwargs)
            if method == "GET" and not url.endswith("/values:batchGet"):
                for sheet in result.body["sheets"]:
                    if sheet["properties"]["title"] == "restaurants":
                        sheet["properties"]["gridProperties"]["rowCount"] = 2105
            return result

    session = PagedSession()
    session.data["restaurants"] = [[] for _ in range(2000)] + [["far-target"]]
    snapshot = SheetsRepository("test-sheet", session).read()
    assert snapshot.tables["restaurants"][0]["id"] == "far-target"
    assert snapshot.row_positions["restaurants"] == [2001]


def test_page_reads_only_requested_finite_data_rectangle():
    session = Session()
    session.data["restaurants"] = [[], ["sample-id"]]
    repository = SheetsRepository("test-sheet", session)
    page = repository.read_page("restaurants", page_size=2)
    assert page["rows"][0]["row_number"] == 3
    assert page["rows"][0]["row"]["id"] == "sample-id"
    data_ranges = [
        a1
        for c in session.calls
        if c[1].endswith("/values:batchGet")
        for a1 in c[2]["params"]["ranges"]
        if not a1.split("!")[1].startswith("A1:")
    ]
    assert data_ranges == ["'restaurants'!A2:S3"]
    assert page["next_row"] is None and page["validation_mode"] == "headers_only"


@pytest.mark.parametrize(
    "table,start,size",
    [("unknown", 2, 100), ("foods", 1, 100), ("foods", 2, 0), ("foods", 2, 1001)],
)
def test_page_invalid_boundaries_do_not_call_api(table, start, size):
    session = Session()
    with pytest.raises(ValueError):
        SheetsRepository("test-sheet", session).read_page(table, start_row=start, page_size=size)
    assert session.calls == []


def test_page_past_grid_is_empty_without_data_request():
    session = Session()
    result = SheetsRepository("test-sheet", session).read_page("foods", start_row=100)
    assert result["rows"] == [] and result["next_row"] is None and result["end_row"] is None
    assert len(session.calls) == 2


def test_safe_get_quota_retry_uses_bounded_backoff(monkeypatch):
    delays = []
    monkeypatch.setattr("travel_db.repository.time.sleep", delays.append)
    monkeypatch.setattr("travel_db.repository.random.random", lambda: 0)

    class QuotaSession:
        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return Response({"ok": True}, status=429 if self.calls < 4 else 200)

    session = QuotaSession()
    result = SheetsRepository("test-sheet", session)._request("GET")
    assert result == {"ok": True} and session.calls == 4
    assert delays == [1, 2, 4]


def test_write_quota_failure_is_not_retried(monkeypatch):
    def forbid_sleep(seconds):
        pytest.fail("POST must not back off and retry")

    monkeypatch.setattr("travel_db.repository.time.sleep", forbid_sleep)

    class QuotaSession:
        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return Response({}, status=429)

    session = QuotaSession()
    with pytest.raises(DatabaseError, match="HTTP 429"):
        SheetsRepository("test-sheet", session)._request(
            "POST", ":batchUpdate", json={"requests": []}
        )
    assert session.calls == 1


def test_read_quota_retry_is_finite(monkeypatch):
    delays = []
    monkeypatch.setattr("travel_db.repository.time.sleep", delays.append)
    monkeypatch.setattr("travel_db.repository.random.random", lambda: 0)

    class QuotaSession:
        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return Response({}, status=429)

    session = QuotaSession()
    with pytest.raises(DatabaseError):
        SheetsRepository("test-sheet", session)._request("GET")
    assert session.calls == 7 and delays == [1, 2, 4, 8, 16, 32]


def test_large_allocated_grid_batches_stay_bounded():
    import re

    class LargeGridSession(Session):
        def request(self, method, url, **kwargs):
            result = super().request(method, url, **kwargs)
            if method == "GET" and not url.endswith("/values:batchGet"):
                for sheet in result.body["sheets"]:
                    if sheet["properties"]["title"] == "restaurants":
                        sheet["properties"]["gridProperties"]["rowCount"] = 30_001
            return result

    session = LargeGridSession()
    snapshot = SheetsRepository("test-sheet", session).read()
    assert snapshot.tables["restaurants"] == []
    bundles = []
    for _method, url, kwargs in session.calls:
        if not url.endswith("/values:batchGet"):
            continue
        cells = 0
        data = False
        for a1 in kwargs["params"]["ranges"]:
            title, coords = a1.split("!")
            if coords.startswith("A1:"):
                continue
            data = True
            start, end = map(int, re.findall(r"\d+", coords))
            assert 1 <= end - start + 1 <= 1000
            cells += (end - start + 1) * len(HEADERS[title.strip("'")])
        if data:
            bundles.append(cells)
    assert len(bundles) >= 3 and max(bundles) <= 200_000
