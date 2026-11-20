"""In-memory Data API (PostgREST) look-alike for the Phase 18 merchants/admin/billing tests.

Supports the query shapes billing.py and the two services use: select (with count="exact"),
eq / neq / in_ / gt / gte / lt / lte, order, limit, range, insert (id + created_at filled in,
monotonically increasing so "newest first" is deterministic), update and delete. Unknown tables
are empty. A `None` column never matches a comparison, like SQL NULL.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

_OPS = {
    "eq": lambda a, b: a == b, "neq": lambda a, b: a != b, "in": lambda a, b: a in b,
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
}
_DB_DEFAULTS = {
    "subscriptions": {"payment_status": "pending", "billing_period": "monthly"},
    "merchant_locations": {},
}
_BASE = datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.count = "select", None, None
        self.filters, self.order_by, self.lim, self.rng = [], None, None, None

    def select(self, _cols="*", count=None):
        self.op, self.count = "select", count
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, patch):
        self.op, self.payload = "update", patch
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _f(self, col, op, val):
        self.filters.append((col, op, val))
        return self

    def eq(self, col, val):
        return self._f(col, "eq", val)

    def neq(self, col, val):
        return self._f(col, "neq", val)

    def in_(self, col, vals):
        return self._f(col, "in", list(vals))

    def gt(self, col, val):
        return self._f(col, "gt", val)

    def gte(self, col, val):
        return self._f(col, "gte", val)

    def lt(self, col, val):
        return self._f(col, "lt", val)

    def lte(self, col, val):
        return self._f(col, "lte", val)

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def limit(self, n):
        self.lim = n
        return self

    def range(self, lo, hi):
        self.rng = (lo, hi)
        return self

    def _match(self, row):
        for col, op, val in self.filters:
            cur = row.get(col)
            if cur is None or not _OPS[op](cur, val):
                return False
        return True

    def execute(self):
        rows = self.db.rows(self.name)
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            made = [self.db.add(self.name, dict(i)) for i in items]
            return SimpleNamespace(data=[dict(m) for m in made], count=None)
        hit = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hit], count=None)
        if self.op == "delete":
            for r in hit:
                rows.remove(r)
            return SimpleNamespace(data=[dict(r) for r in hit], count=None)
        if self.order_by:
            col, desc = self.order_by
            hit.sort(key=lambda r: (r.get(col) is None, str(r.get(col))), reverse=desc)
        total = len(hit)
        if self.rng:
            hit = hit[self.rng[0]:self.rng[1] + 1]
        if self.lim is not None:
            hit = hit[:self.lim]
        return SimpleNamespace(data=[dict(r) for r in hit],
                               count=total if self.count else None)


class FakeData:
    """The client. `tables` is the live store, so a test can assert on it directly."""

    def __init__(self, **tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self._n = 0
        self.fail_inserts: set[str] = set()     # table names whose insert raises

    def rows(self, name):
        return self.tables.setdefault(name, [])

    def add(self, name, row):
        if name in self.fail_inserts:
            raise RuntimeError(f"insert into {name} failed")
        self._n += 1
        for col, val in _DB_DEFAULTS.get(name, {}).items():      # what the schema fills in
            row.setdefault(col, val)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", (_BASE + timedelta(seconds=self._n)).isoformat())
        self.rows(name).append(row)
        return row

    def table(self, name):
        return _Query(self, name)
