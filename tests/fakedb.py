"""A tiny in-memory PostgREST look-alike for the Phase 16 tests (no network).

Supports the query shapes the notify engine and services use: select/insert/update/upsert/
delete with eq/in_/gt/gte/lt/lte filters, order/limit/range, count="exact", and `rpc`
handlers registered by name. Timestamps compare as ISO strings (all UTC in tests).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

_OPS = {"eq": lambda a, b: a == b, "in": lambda a, b: a in b, "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}


class UniqueViolation(Exception):
    pass


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.opts = "select", None, {}
        self.filters, self.order_by, self.lim, self.rng, self.count = [], None, None, None, None

    # builders ---------------------------------------------------------------------
    def select(self, _cols="*", count=None):
        self.op, self.count = "select", count
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, patch):
        self.op, self.payload = "update", patch
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload = "upsert", rows
        self.opts = {"on_conflict": on_conflict, "ignore": ignore_duplicates}
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append((col, "eq", val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, "in", list(vals)))
        return self

    def gt(self, col, val):
        self.filters.append((col, "gt", val))
        return self

    def gte(self, col, val):
        self.filters.append((col, "gte", val))
        return self

    def lt(self, col, val):
        self.filters.append((col, "lt", val))
        return self

    def lte(self, col, val):
        self.filters.append((col, "lte", val))
        return self

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def limit(self, n):
        self.lim = n
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    # execution --------------------------------------------------------------------
    def _match(self, row):
        for col, op, val in self.filters:
            cur = row.get(col)
            if cur is None:
                return False
            ok = _OPS[op](cur, val)
            if not ok:
                return False
        return True

    def execute(self):
        rows = self.db.rows(self.name)
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            made = [self.db.insert(self.name, dict(i)) for i in items]
            return SimpleNamespace(data=made, count=None)
        if self.op == "upsert":
            return SimpleNamespace(data=self.db.upsert(self.name, self.payload, self.opts),
                                   count=None)
        hit = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hit], count=None)
        if self.op == "delete":
            for r in hit:
                rows.remove(r)
            return SimpleNamespace(data=hit, count=None)
        if self.order_by:
            col, desc = self.order_by
            hit.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        total = len(hit)
        if self.rng:
            hit = hit[self.rng[0]:self.rng[1] + 1]
        if self.lim is not None:
            hit = hit[:self.lim]
        return SimpleNamespace(data=[dict(r) for r in hit],
                               count=total if self.count else None)


class FakeDB:
    ALIASES = {"notification_history_view": "notifications"}

    def __init__(self, **tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.rpcs: dict = {}
        self.rpc_calls: list = []

    def rows(self, name):
        return self.tables.setdefault(self.ALIASES.get(name, name), [])

    def table(self, name):
        q = Query(self, name)
        if name == "notification_history_view":
            q.execute = self._history(q)
        return q

    def _history(self, q):
        base = Query.execute

        def run():
            res = base(q)
            for r in res.data:
                r.setdefault("recipient_count", r.get("recipients", 0))
                r.setdefault("delivered", 0)
                r.setdefault("visits_48h", 0)
            return res
        return run

    def insert(self, name, row):
        name = self.ALIASES.get(name, name)
        row.setdefault("id", str(uuid.uuid4()))
        if name == "notification_deliveries":
            row.setdefault("status", "pending")
            row.setdefault("attempts", 0)
            self._unique(name, row, "dedupe_key")
        if name == "notifications":
            row.setdefault("status", "queued")
            row.setdefault("recipients", 0)
            row.setdefault("created_at", "2026-11-11T12:00:00+00:00")
            if row.get("source") == "automation":
                clash = [r for r in self.rows(name) if r.get("source") == "automation"
                         and (r["merchant_id"], r["type"], r["day"])
                         == (row["merchant_id"], row["type"], row["day"])]
                if clash:
                    raise UniqueViolation("notifications_automation_day_uidx")
        self.rows(name).append(row)
        return dict(row)

    @staticmethod
    def _key(row, key):
        return row.get(key)

    def _unique(self, name, row, key):
        if row.get(key) is not None and any(
                r.get(key) == row[key] for r in self.rows(name)):
            raise UniqueViolation(key)

    def upsert(self, name, payload, opts):
        items = payload if isinstance(payload, list) else [payload]
        key, out = opts["on_conflict"], []
        for item in items:
            existing = [r for r in self.rows(name) if r.get(key) == item.get(key)]
            if existing:
                if not opts["ignore"]:
                    existing[0].update(item)
                    out.append(dict(existing[0]))
                continue
            out.append(self.insert(name, dict(item)))
        return out

    def rpc(self, name, params=None, count=None):
        self.rpc_calls.append((name, params))
        data = self.rpcs[name](params)

        class _R:
            def limit(self, _n):
                return self

            def execute(self):
                return SimpleNamespace(data=data, count=len(data) if count else None)
        return _R()
