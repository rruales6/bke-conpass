"""Idempotency store guarantees (offline-replayable cashier ops, B5)."""
import json
import uuid
from datetime import datetime

import pytest
from conpass_common.errors import Conflict
from conpass_common.idempotency import (
    IdempotencyStore,
    InMemoryIdempotencyStore,
    StoredResponse,
    run_idempotent,
)


def test_computes_once_then_replays():
    store = InMemoryIdempotencyStore()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return StoredResponse(200, {"result": calls["n"]})

    payload = {"cardId": "c1", "stamps": 1}
    r1 = run_idempotent(store, key="k1", endpoint="accrue", payload=payload, compute=compute)
    r2 = run_idempotent(store, key="k1", endpoint="accrue", payload=payload, compute=compute)

    assert calls["n"] == 1                 # computed exactly once
    assert r1.body == r2.body == {"result": 1}


class _CaptureTable:
    """Minimal fake of the supabase table builder that records the inserted row."""
    def __init__(self, sink):
        self._sink = sink

    def insert(self, row):
        self._sink["row"] = row
        return self

    def execute(self):
        # Mirror the Data API: the row is JSON-encoded on the wire. This raises
        # TypeError if the body still holds UUID/datetime objects.
        json.dumps(self._sink["row"])
        return self


def test_put_persists_uuid_and_datetime_bodies():
    # Regression: accrue/redeem response bodies carry UUID (format: uuid) and
    # date-time fields; the store must normalize them to JSON-safe primitives,
    # else put() 500s AFTER the mutation committed and replays re-apply it.
    sink: dict = {}
    store = IdempotencyStore(client=None)
    store._client = type("C", (), {"table": lambda _self, _name: _CaptureTable(sink)})()
    cid = uuid.uuid4()
    body = {"card": {"id": cid, "createdAt": datetime(2026, 7, 22, 12, 0, 0)}}

    store.put("k1", "accrue", "hash", StoredResponse(200, body))  # must not raise

    row = sink["row"]
    assert row["response_body"]["card"]["id"] == str(cid)
    json.dumps(row["response_body"])  # fully JSON-serializable


def test_same_key_different_payload_conflicts():
    store = InMemoryIdempotencyStore()
    run_idempotent(store, key="k1", endpoint="accrue",
                   payload={"stamps": 1}, compute=lambda: StoredResponse(200, {}))
    with pytest.raises(Conflict):
        run_idempotent(store, key="k1", endpoint="accrue",
                       payload={"stamps": 2}, compute=lambda: StoredResponse(200, {}))
