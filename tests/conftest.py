import json
from copy import deepcopy
from pathlib import Path

import pytest

from travel_db.repository import ConflictError, DatabaseError, Snapshot, planned_snapshot
from travel_db.schema import HEADERS


class MemoryRepository:
    """Test-only repository: atomic replacement, never used by application commands."""

    def __init__(self):
        self.tables = {title: [] for title in HEADERS}
        self.commits = 0
        self.fail_commit = False

    def read(self):
        return Snapshot(deepcopy(self.tables))

    def commit(self, additions, expected, *, updates=None):
        if self.read().digest != expected.digest:
            raise ConflictError("concurrent change")
        if self.fail_commit:
            raise DatabaseError("simulated atomic write failure")
        self.tables = planned_snapshot(self.read(), additions, updates).tables
        self.commits += 1


@pytest.fixture
def repository():
    return MemoryRepository()


@pytest.fixture
def sample():
    return json.loads((Path(__file__).parents[1] / "examples/sample.json").read_text())
