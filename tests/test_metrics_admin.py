"""Phase 6 tests — program metrics, redemptions report, and the admin console
(cross-tenant clients/stats/subscription PATCH). All network is mocked: a small fake
Supabase Data API client supports the query-builder chains these handlers use
(select/eq/gte/lte/order/update/execute), extending the _FakeQuery pattern from
test_wallet.py. Uses monkeypatch so patches never leak across modules."""
from __future__ import annotations

from conpass_common.app import require_identity
from conpass_common.auth import Identity
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
P1 = "33333333-3333-3333-3333-333333333333"


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# Fake Supabase Data API client — supports the chains used across these handlers.
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, rows, table_name, store):
        self._rows = list(rows)
        self._table_name = table_name
        self._store = store
        self._patch: dict | None = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def gte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] >= val]
        return self

    def lte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] <= val]
        return self

    def lt(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] < val]
        return self

    def order(self, col, desc=False):
        self._rows = sorted(self._rows, key=lambda r: r.get(col), reverse=desc)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def update(self, patch):
        self._patch = patch
        return self

    def execute(self):
        if self._patch is not None:
            table = self._store.setdefault(self._table_name, [])
            updated = []
            for r in table:
                if r in self._rows:
                    r.update(self._patch)
                    updated.append(r)
            return type("R", (), {"data": updated})()
        return type("R", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, tables):
        self._t = {k: list(v) for k, v in tables.items()}

    def table(self, name):
        return _FakeQuery(self._t.get(name, []), name, self._t)


def _admin_identity(app, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="admin@conpass.cards", roles=["platform_admin"]))


# --------------------------------------------------------------------------- #
# 1. Program metrics
# --------------------------------------------------------------------------- #
def test_program_metrics_maps_view_row(monkeypatch):
    from services.programs import handler as h

    tables = {
        "programs": [{"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                      "name": "Café", "created_at": "2026-07-18T00:00:00Z"}],
        "program_metrics_view": [{
            "program_id": P1, "visits": 42, "redemptions": 5,
            "active_installed_passes": 30, "installs_this_week": 4,
            "eligible_for_reminder": 3, "churn_rate": 12.5,
            "second_visit_rate_30d": 66.7,
        }],
    }
    monkeypatch.setattr(h, "get_repo", lambda: type(
        "R", (), {"get": staticmethod(lambda pid: tables["programs"][0])})())
    monkeypatch.setattr(h, "service_client", lambda: _FakeClient(tables))
    _identity(h.app, monkeypatch)

    r = TestClient(h.app).get(f"/programs/{P1}/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "visits": 42, "redemptions": 5, "activeInstalledPasses": 30,
        "installsThisWeek": 4, "eligibleForReminder": 3,
        "churnRate": 12.5, "secondVisitRate30d": 66.7,
    }


def test_program_metrics_zero_fallback_when_no_row(monkeypatch):
    from services.programs import handler as h

    tables = {
        "programs": [{"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                      "name": "Café", "created_at": "2026-07-18T00:00:00Z"}],
        "program_metrics_view": [],
    }
    monkeypatch.setattr(h, "get_repo", lambda: type(
        "R", (), {"get": staticmethod(lambda pid: tables["programs"][0])})())
    monkeypatch.setattr(h, "service_client", lambda: _FakeClient(tables))
    _identity(h.app, monkeypatch)

    r = TestClient(h.app).get(f"/programs/{P1}/metrics")
    assert r.status_code == 200
    assert r.json() == {
        "visits": 0, "redemptions": 0, "activeInstalledPasses": 0,
        "installsThisWeek": 0, "eligibleForReminder": 0,
        "churnRate": 0.0, "secondVisitRate30d": 0.0,
    }


def test_program_metrics_requires_matching_merchant(monkeypatch):
    from services.programs import handler as h

    program = {"id": P1, "merchant_id": M2, "type": "loyalty_stamps",
               "name": "Café", "created_at": "2026-07-18T00:00:00Z"}
    monkeypatch.setattr(h, "get_repo", lambda: type(
        "R", (), {"get": staticmethod(lambda pid: program)})())
    _identity(h.app, monkeypatch, merchant_id=M1)

    r = TestClient(h.app).get(f"/programs/{P1}/metrics")
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# 2. Redemptions report
# --------------------------------------------------------------------------- #
def test_redemptions_maps_rows(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.programs[P1] = {"id": P1, "merchant_id": M1}
    repo.redemptions = [
        {"id": "r1", "customer_name": "Juan R.", "customer_phone": "+593999",
         "customer_email": None, "program_id": P1, "program": "Café",
         "reward": "Café gratis", "redeemed_at": "2026-07-10T10:00:00Z",
         "merchant_id": M1},
        {"id": "r2", "customer_name": "Maria T.", "customer_phone": None,
         "customer_email": "m@x.co", "program_id": P1, "program": "Café",
         "reward": "Café gratis", "redeemed_at": "2026-07-15T10:00:00Z",
         "merchant_id": M1},
    ]
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _identity(h.app, monkeypatch, roles=("merchant_owner",))

    r = TestClient(h.app).get(f"/programs/{P1}/redemptions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    # ordered by redeemed_at desc
    assert body[0]["id"] == "r2"
    assert body[0]["customerName"] == "Maria T."
    assert body[0]["customerEmail"] == "m@x.co"
    assert "customerPhone" not in body[0]
    assert body[1]["customerPhone"] == "+593999"
    assert "customerEmail" not in body[1]
    assert body[1]["programId"] == P1
    assert body[1]["program"] == "Café"
    assert body[1]["reward"] == "Café gratis"


def test_redemptions_applies_date_filter(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.programs[P1] = {"id": P1, "merchant_id": M1}
    repo.redemptions = [
        {"id": "r1", "customer_name": "Juan R.", "program_id": P1, "program": "Café",
         "reward": "Café gratis", "redeemed_at": "2026-07-01T10:00:00Z",
         "merchant_id": M1},
        {"id": "r2", "customer_name": "Maria T.", "program_id": P1, "program": "Café",
         "reward": "Café gratis", "redeemed_at": "2026-07-15T10:00:00Z",
         "merchant_id": M1},
    ]
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _identity(h.app, monkeypatch, roles=("merchant_owner",))

    r = TestClient(h.app).get(
        f"/programs/{P1}/redemptions",
        params={"from": "2026-07-10", "to": "2026-07-31"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "r2"


def test_redemptions_requires_matching_merchant(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.programs[P1] = {"id": P1, "merchant_id": M2}
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _identity(h.app, monkeypatch, roles=("merchant_owner",), merchant_id=M1)

    r = TestClient(h.app).get(f"/programs/{P1}/redemptions")
    assert r.status_code == 403


def test_redemptions_missing_program_404(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _identity(h.app, monkeypatch, roles=("merchant_owner",))

    r = TestClient(h.app).get(f"/programs/{P1}/redemptions")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# 3. Admin — clients, stats, subscription PATCH
# --------------------------------------------------------------------------- #
def _admin_tables():
    return {
        "merchants": [
            {"id": M1, "business_name": "Café Uno", "city": "Quito",
             "created_at": "2026-07-01T00:00:00Z"},
            {"id": M2, "business_name": "Gym Dos", "city": "Cuenca",
             "created_at": "2026-07-05T00:00:00Z"},
        ],
        "subscriptions": [
            {"merchant_id": M1, "tier": "growth", "payment_status": "paid",
             "mrr_usd": 49, "active_pass_limit": 1500, "program_limit": 3,
             "operation_user_limit": 5, "next_charge_at": "2026-08-01",
             "last_payment_at": "2026-07-01"},
            {"merchant_id": M2, "tier": "starter", "payment_status": "overdue",
             "mrr_usd": 19, "active_pass_limit": 250, "program_limit": 1,
             "operation_user_limit": 1, "next_charge_at": "2026-07-20",
             "last_payment_at": None},
        ],
        "merchant_active_passes": [
            {"merchant_id": M1, "active_pass_count": 120},
        ],
    }


def test_admin_clients_join(monkeypatch):
    from services.admin import handler as h

    tables = _admin_tables()
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_FakeClient(tables)))
    _admin_identity(h.app, monkeypatch)

    r = TestClient(h.app).get("/admin/clients")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    # ordered by merchant created_at desc
    assert body[0]["merchantId"] == M2
    assert body[0]["name"] == "Gym Dos"
    assert body[0]["city"] == "Cuenca"
    assert body[0]["tier"] == "starter"
    assert body[0]["paymentStatus"] == "overdue"
    assert body[0]["mrrUsd"] == 19.0
    assert "lastPaymentAt" not in body[0]

    c1 = next(c for c in body if c["merchantId"] == M1)
    assert c1["tier"] == "growth"
    assert c1["mrrUsd"] == 49.0
    assert c1["nextChargeAt"] == "2026-08-01"
    assert c1["lastPaymentAt"] == "2026-07-01"


def test_admin_clients_requires_platform_admin(monkeypatch):
    from services.admin import handler as h

    _identity(h.app, monkeypatch, roles=("merchant_owner",))
    r = TestClient(h.app).get("/admin/clients")
    assert r.status_code == 403


def test_admin_stats_aggregation(monkeypatch):
    from services.admin import handler as h

    tables = _admin_tables()
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_FakeClient(tables)))
    _admin_identity(h.app, monkeypatch)

    r = TestClient(h.app).get("/admin/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["activeClients"] == 1          # only M1 is paid
    assert body["mrrUsd"] == 49.0               # sum of paid mrr_usd only
    assert body["growthCount"] == 1
    assert body["starterCount"] == 1
    assert body["overdueCount"] == 1


def test_admin_subscription_patch_updates_and_returns_active_pass_count(monkeypatch):
    from services.admin import handler as h

    tables = _admin_tables()
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_FakeClient(tables)))
    _admin_identity(h.app, monkeypatch)

    r = TestClient(h.app).patch(
        f"/admin/clients/{M2}/subscription",
        json={"paymentStatus": "paid"})
    assert r.status_code == 200
    body = r.json()
    assert body["paymentStatus"] == "paid"
    assert body["tier"] == "starter"            # unchanged field preserved
    assert body["activePassCount"] == 0         # no row in merchant_active_passes for M2
    assert body["mrrUsd"] == 19.0


def test_admin_subscription_patch_tier_change(monkeypatch):
    from services.admin import handler as h

    tables = _admin_tables()
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_FakeClient(tables)))
    _admin_identity(h.app, monkeypatch)

    r = TestClient(h.app).patch(
        f"/admin/clients/{M1}/subscription",
        json={"tier": "pro"})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "pro"
    assert body["paymentStatus"] == "paid"      # unchanged
    assert body["activePassCount"] == 120


def test_admin_subscription_patch_missing_merchant_404(monkeypatch):
    from services.admin import handler as h

    tables = _admin_tables()
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_FakeClient(tables)))
    _admin_identity(h.app, monkeypatch)

    other = "77777777-7777-7777-7777-777777777777"
    r = TestClient(h.app).patch(
        f"/admin/clients/{other}/subscription",
        json={"tier": "pro"})
    assert r.status_code == 404
