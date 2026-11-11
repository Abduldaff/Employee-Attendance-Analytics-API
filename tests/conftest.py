"""Tests run against a REAL MongoDB when MONGO_URI is set (recommended), else against mongomock
(mongomock cannot run $setWindowFields / $densify / explain, so those tests are skipped)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

USING_MOCK = not os.getenv("MONGO_URI")
if USING_MOCK:
    import mongomock

    os.environ["MONGO_URI"] = "mongodb://mock-host:27017"
    _patch = mongomock.patch(servers=(("mock-host", 27017),))
    _patch.start()
os.environ.setdefault("MONGO_DB", "attendance_test")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402


@pytest.fixture()
def api():
    m.client.drop_database(m.db.name)
    m.ensure_indexes()
    return TestClient(m.app)


requires_real_mongo = pytest.mark.skipif(USING_MOCK, reason="needs a real MongoDB 6+/7")
