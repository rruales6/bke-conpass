"""Idempotency store guarantees (offline-replayable cashier ops, B5)."""
import pytest
from conpass_common.errors import Conflict
from conpass_common.idempotency import (
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


def test_same_key_different_payload_conflicts():
    store = InMemoryIdempotencyStore()
    run_idempotent(store, key="k1", endpoint="accrue",
                   payload={"stamps": 1}, compute=lambda: StoredResponse(200, {}))
    with pytest.raises(Conflict):
        run_idempotent(store, key="k1", endpoint="accrue",
                       payload={"stamps": 2}, compute=lambda: StoredResponse(200, {}))
