"""Idempotency for mutating operations (B5: offline-replayable cashier ops).

The client sends a UUID `Idempotency-Key`. The first successful execution stores its
serialized response keyed by that UUID; any replay (e.g. after the cashier device comes
back online) returns the identical stored response instead of re-applying the mutation.

A request hash guards against key reuse with a different payload.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import Conflict
from .logging import get_logger

log = get_logger(__name__)


def request_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass
class StoredResponse:
    status: int
    body: dict


class IdempotencyStore:
    """Thin wrapper over the idempotency_records table. Injected so it can be faked
    in tests without a database."""

    def __init__(self, client=None):
        self._client = client

    def _table(self):
        if self._client is None:
            from .db import service_client
            self._client = service_client()
        return self._client.table("idempotency_records")

    def get(self, key: str) -> tuple[StoredResponse, str] | None:
        rows = self._table().select("*").eq("idempotency_key", key).execute().data
        if not rows:
            return None
        r = rows[0]
        return StoredResponse(r["response_status"], r["response_body"]), r["request_hash"]

    def put(self, key: str, endpoint: str, req_hash: str, resp: StoredResponse) -> None:
        self._table().insert({
            "idempotency_key": key,
            "endpoint": endpoint,
            "request_hash": req_hash,
            "response_status": resp.status,
            "response_body": resp.body,
        }).execute()


class InMemoryIdempotencyStore(IdempotencyStore):
    """No-DB store for tests and `serverless offline` smoke runs."""

    def __init__(self):
        self._data: dict[str, tuple[StoredResponse, str]] = {}

    def get(self, key: str):
        return self._data.get(key)

    def put(self, key: str, endpoint: str, req_hash: str, resp: StoredResponse) -> None:
        self._data[key] = (resp, req_hash)


def run_idempotent(
    store: IdempotencyStore,
    *,
    key: str,
    endpoint: str,
    payload: Any,
    compute: Callable[[], StoredResponse],
) -> StoredResponse:
    """Return the stored response for `key`, or compute+store it once."""
    rhash = request_hash(payload)
    existing = store.get(key)
    if existing is not None:
        stored, prev_hash = existing
        if prev_hash != rhash:
            raise Conflict("Idempotency-Key reused with a different request body")
        log.info("idempotent_replay", extra={"endpoint": endpoint, "key": key})
        return stored
    result = compute()
    store.put(key, endpoint, rhash, result)
    return result
