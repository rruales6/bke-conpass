"""Minimal in-memory Supabase Data API fake for the Phase 16 tests: select/eq/in_/gte/
order/range/limit/insert/update/delete (+ count="exact"). Enough for the query chains the
locations/usage/admin code uses; not a general PostgREST emulator."""
from __future__ import annotations

import itertools
from types import SimpleNamespace

_ids = itertools.count(1)


class _Query:
    def __init__(self, store, name):
        self._store, self._name = store, name
        self._rows = list(store.setdefault(name, []))
        self._op = "select"
        self._payload = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def gte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] >= val]
        return self

    def order(self, col, desc=False):
        self._rows = sorted(self._rows, key=lambda r: str(r.get(col)), reverse=desc)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def range(self, lo, hi):
        self._rows = self._rows[lo:hi + 1]
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        table = self._store[self._name]
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for r in rows:
                row = dict(r)
                if "id" not in row and self._name != "operation_user_locations":
                    row["id"] = f"00000000-0000-0000-0000-{next(_ids):012d}"
                row.setdefault("created_at", "2026-11-11T00:00:00+00:00")
                table.append(row)
                out.append(row)
            return SimpleNamespace(data=out, count=None)
        if self._op == "update":
            for r in self._rows:
                r.update(self._payload)
            return SimpleNamespace(data=self._rows, count=None)
        if self._op == "delete":
            for r in self._rows:
                table.remove(r)
            return SimpleNamespace(data=self._rows, count=None)
        return SimpleNamespace(data=self._rows, count=len(self._rows))


class FakeClient:
    def __init__(self, tables=None):
        self.tables = {k: list(v) for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self.tables, name)
