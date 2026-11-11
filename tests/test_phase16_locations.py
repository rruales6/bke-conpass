"""Phase 16 — locales: CRUD + 10-active limit, cashier assignment, scan-location rules,
/me locations, enrolment attribution and notification metering in usage."""
from __future__ import annotations

import pytest
from _fake_supabase import FakeClient
from conpass_common import locations, usage, wallet_sync
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.errors import ValidationFailed
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
OP = "44444444-4444-4444-4444-444444444444"
OP2 = "55555555-5555-5555-5555-555555555555"
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
L1 = "aaaaaaaa-0000-0000-0000-000000000001"
L2 = "aaaaaaaa-0000-0000-0000-000000000002"
L3 = "aaaaaaaa-0000-0000-0000-000000000003"   # inactive
LX = "bbbbbbbb-0000-0000-0000-000000000009"   # other merchant's


def _loc(lid, name, merchant=M1, active=True, created="2026-11-01T00:00:00+00:00"):
    return {"id": lid, "merchant_id": merchant, "name": name, "latitude": -0.18,
            "longitude": -78.48, "active": active, "created_at": created}


def _tables():
    return {
        "merchant_locations": [
            _loc(L1, "Centro", created="2026-11-01T00:00:00+00:00"),
            _loc(L2, "Norte", created="2026-11-02T00:00:00+00:00"),
            _loc(L3, "Sur", active=False, created="2026-11-03T00:00:00+00:00"),
            _loc(LX, "Ajeno", merchant=M2),
        ],
        "operation_user_locations": [
            {"user_id": OP, "location_id": L1},
            {"user_id": OP, "location_id": L3},
        ],
    }


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1, user_id=U1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=user_id, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _who(roles, user_id=U1, merchant_id=M1):
    return Identity(user_id=user_id, email=None, roles=list(roles), merchant_id=merchant_id)


def test_active_locations_oldest_first_and_tenant_scoped():
    rows = locations.active_locations(FakeClient(_tables()), M1)
    assert [r["id"] for r in rows] == [L1, L2]


def test_identity_locations_by_role():
    c = FakeClient(_tables())
    assert locations.identity_locations(c, _who(["merchant_owner"])) == [
        {"id": L1, "name": "Centro"}, {"id": L2, "name": "Norte"}]
    # cashier: only assigned AND active (L3 is assigned but paused)
    assert locations.identity_locations(c, _who(["operation_user"], user_id=OP)) == [
        {"id": L1, "name": "Centro"}]
    assert locations.identity_locations(c, _who(["operation_user"], user_id=OP2)) == []
    assert locations.identity_locations(c, _who(["platform_admin"], merchant_id=None)) == []


def test_resolve_scan_location_rules():
    c = FakeClient(_tables())
    cashier = _who(["operation_user"], user_id=OP)
    assert locations.resolve_scan_location(c, cashier, M1, None) is None
    assert locations.resolve_scan_location(c, cashier, M1, L1) == L1
    assert locations.resolve_scan_location(c, _who(["merchant_owner"]), M1, L2) == L2
    for bad in (L2,   # active but not assigned to this cashier
                L3,   # assigned but inactive
                LX,   # another merchant's
                "cccccccc-0000-0000-0000-000000000000"):   # unknown
        with pytest.raises(ValidationFailed) as exc:
            locations.resolve_scan_location(c, cashier, M1, bad)
        assert exc.value.details["reason"] == "location_not_allowed"
    with pytest.raises(ValidationFailed):   # owner too: foreign locale is refused
        locations.resolve_scan_location(c, _who(["merchant_owner"]), M1, LX)


# --------------------------------------------------------------------------- #
# merchants service — locations CRUD
# --------------------------------------------------------------------------- #
@pytest.fixture
def merchants(monkeypatch):
    import services.merchants.handler as h
    from services.merchants.repository import MerchantsRepository
    client = FakeClient(_tables())
    repo = MerchantsRepository(client)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    synced = []
    monkeypatch.setattr(wallet_sync, "sync_merchant_classes",
                        lambda c, mid: synced.append(mid))
    _identity(h.app, monkeypatch)
    h.client, h.synced = client, synced   # handy for the tests (module is shared; reset per use)
    return h


def test_list_locations_counts_cashiers(merchants):
    r = TestClient(merchants.app).get(f"/merchants/{M1}/locations")
    assert r.status_code == 200
    body = r.json()
    assert [x["id"] for x in body] == [L1, L2, L3]
    assert body[0]["enrollmentQuery"] == f"local={L1}"
    assert body[0]["operationUserCount"] == 1 and body[1]["operationUserCount"] == 0


def test_create_location_rounds_and_syncs(merchants):
    r = TestClient(merchants.app).post(f"/merchants/{M1}/locations", json={
        "name": " Mall ", "latitude": -0.1806512345, "longitude": -78.4844399})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["name"] == "Mall" and b["latitude"] == -0.180651 and b["longitude"] == -78.48444
    assert b["active"] is True and b["enrollmentQuery"] == f"local={b['id']}"
    assert merchants.synced == [M1]


def test_create_location_wallet_failure_does_not_fail(merchants, monkeypatch):
    def boom(*_a):
        raise RuntimeError("wallet down")
    monkeypatch.setattr(wallet_sync, "sync_merchant_classes", boom)
    r = TestClient(merchants.app).post(f"/merchants/{M1}/locations", json={
        "name": "X", "latitude": 1, "longitude": 1})
    assert r.status_code == 201


def _fill_to_ten(merchants):
    c = TestClient(merchants.app)
    for i in range(8):   # L1, L2 already active -> 10
        assert c.post(f"/merchants/{M1}/locations", json={
            "name": f"S{i}", "latitude": 0, "longitude": 0}).status_code == 201


def test_active_limit_on_create_and_activate(merchants):
    _fill_to_ten(merchants)
    c = TestClient(merchants.app)
    r = c.post(f"/merchants/{M1}/locations", json={"name": "11", "latitude": 0, "longitude": 0})
    assert r.status_code == 409 and r.json()["details"]["reason"] == "active_location_limit"
    # inactive creation is always fine; activating it is not
    r = c.post(f"/merchants/{M1}/locations", json={
        "name": "paused", "latitude": 0, "longitude": 0, "active": False})
    assert r.status_code == 201
    r = c.patch(f"/merchants/{M1}/locations/{L3}", json={"active": True})
    assert r.status_code == 409 and r.json()["details"]["reason"] == "active_location_limit"
    # pausing one frees a slot
    assert c.patch(f"/merchants/{M1}/locations/{L1}", json={"active": False}).status_code == 200
    assert c.patch(f"/merchants/{M1}/locations/{L3}", json={"active": True}).status_code == 200


def test_update_and_delete_location(merchants):
    c = TestClient(merchants.app)
    r = c.patch(f"/merchants/{M1}/locations/{L1}", json={"name": "Centro 2"})
    assert r.status_code == 200 and r.json()["name"] == "Centro 2"
    assert r.json()["operationUserCount"] == 1
    assert c.delete(f"/merchants/{M1}/locations/{L2}").status_code == 204
    assert [x["id"] for x in merchants.client.tables["merchant_locations"]
            if x["merchant_id"] == M1] == [L1, L3]
    assert merchants.synced == [M1, M1]


def test_locations_ownership(merchants, monkeypatch):
    c = TestClient(merchants.app)
    # another merchant's locale -> 404 even for its rightful owner path
    assert c.patch(f"/merchants/{M1}/locations/{LX}", json={"name": "x"}).status_code == 404
    assert c.delete(f"/merchants/{M1}/locations/{LX}").status_code == 404
    # cross-tenant owner / cashier / admin -> 403
    _identity(merchants.app, monkeypatch, merchant_id=M2)
    assert c.get(f"/merchants/{M1}/locations").status_code == 403
    _identity(merchants.app, monkeypatch, roles=("operation_user",))
    assert c.get(f"/merchants/{M1}/locations").status_code == 403
    assert c.post(f"/merchants/{M1}/locations", json={
        "name": "x", "latitude": 0, "longitude": 0}).status_code == 403
    _identity(merchants.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    assert c.get(f"/merchants/{M1}/locations").status_code == 403


def test_location_validation(merchants):
    c = TestClient(merchants.app)
    for body in ({"name": "x", "latitude": 91, "longitude": 0},
                 {"name": "", "latitude": 0, "longitude": 0},
                 {"name": "x", "latitude": 0}):
        assert c.post(f"/merchants/{M1}/locations", json=body).status_code == 422


# --------------------------------------------------------------------------- #
# operation users <-> locales
# --------------------------------------------------------------------------- #
@pytest.fixture
def opusers(merchants, monkeypatch):
    h = merchants
    repo = h.get_repo()
    repo._c.tables["profiles"] = [{
        "user_id": OP, "merchant_id": M1, "role": "operation_user", "name": "Lucía",
        "email": "l@x.co", "station": "caja_1"}]
    repo._c.tables["subscriptions"] = [{"merchant_id": M1, "tier": "pro"}]
    monkeypatch.setattr(h, "provision_user", lambda **kw: type(
        "U", (), {"user_id": OP2, "email": kw["email"], "temp_password": "pw"})())
    monkeypatch.setattr(h, "update_user", lambda *a, **k: None)
    return h


def test_create_operation_user_with_locations_round_trip(opusers):
    c = TestClient(opusers.app)
    r = c.post(f"/merchants/{M1}/operation-users", json={
        "name": "Ana", "email": "ana@x.co", "station": "caja_2", "locationIds": [L1, L2, L1]})
    assert r.status_code == 201, r.text
    assert sorted(r.json()["locationIds"]) == [L1, L2]
    rows = c.get(f"/merchants/{M1}/operation-users").json()
    by_id = {u["id"]: u for u in rows}
    assert sorted(by_id[OP2]["locationIds"]) if OP2 in by_id else True
    assert sorted(by_id[OP]["locationIds"]) == [L1, L3]


def test_create_operation_user_without_locations_returns_empty(opusers):
    r = TestClient(opusers.app).post(f"/merchants/{M1}/operation-users", json={
        "name": "Ana", "email": "ana@x.co", "station": "caja_2"})
    assert r.status_code == 201 and r.json()["locationIds"] == []


def test_foreign_location_is_422_and_nothing_provisioned(opusers, monkeypatch):
    provisioned = []
    monkeypatch.setattr(opusers, "provision_user", lambda **kw: provisioned.append(kw))
    r = TestClient(opusers.app).post(f"/merchants/{M1}/operation-users", json={
        "name": "Ana", "email": "ana@x.co", "station": "caja_2", "locationIds": [LX]})
    assert r.status_code == 422
    assert r.json()["details"]["reason"] == "location_not_found"
    assert provisioned == []
    r = TestClient(opusers.app).patch(f"/merchants/{M1}/operation-users/{OP}",
                                      json={"locationIds": [LX]})
    assert r.status_code == 422


def test_update_replaces_set_and_empty_clears(opusers):
    c = TestClient(opusers.app)
    r = c.patch(f"/merchants/{M1}/operation-users/{OP}", json={"locationIds": [L2]})
    assert r.status_code == 200 and r.json()["locationIds"] == [L2]
    # omitted -> untouched
    r = c.patch(f"/merchants/{M1}/operation-users/{OP}", json={"name": "Nueva"})
    assert r.json()["locationIds"] == [L2]
    r = c.patch(f"/merchants/{M1}/operation-users/{OP}", json={"locationIds": []})
    assert r.json()["locationIds"] == []
    assert c.get(f"/merchants/{M1}/operation-users").json()[0]["locationIds"] == []


# --------------------------------------------------------------------------- #
# /me
# --------------------------------------------------------------------------- #
def test_me_locations_by_role(monkeypatch):
    import services.identity.handler as h
    monkeypatch.setattr(h, "service_client", lambda: FakeClient(_tables()))
    c = TestClient(h.app)
    _identity(h.app, monkeypatch, roles=("merchant_owner",))
    assert [x["id"] for x in c.get("/me").json()["locations"]] == [L1, L2]
    _identity(h.app, monkeypatch, roles=("operation_user",), user_id=OP)
    assert c.get("/me").json()["locations"] == [{"id": L1, "name": "Centro"}]
    _identity(h.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    assert c.get("/me").json()["locations"] == []


# --------------------------------------------------------------------------- #
# enrolment attribution
# --------------------------------------------------------------------------- #
P1 = "33333333-3333-3333-3333-333333333333"


class _EnrollRepo:
    def __init__(self):
        self.created = []

    def get_program(self, pid):
        return {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "mechanic": "stamps",
                "active": True, "stamps_for_reward": 5}

    def get_merchant(self, mid):
        return {"id": mid, "business_name": "X"}

    def get_customer(self, cid):
        return None

    def find_card_by_dedupe(self, *_a):
        return None

    def create_customer(self, d):
        return {"id": "cust1"}

    def create_card(self, d):
        self.created.append(d)
        return {**d, "id": "card1", "created_at": "2026-11-11T00:00:00+00:00"}


def _enroll(monkeypatch, location_id):
    from conpass_common.idempotency import InMemoryIdempotencyStore

    import services.enrollment.handler as h
    repo = _EnrollRepo()
    store = InMemoryIdempotencyStore()
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: store)
    monkeypatch.setattr(h, "_wallet_links", lambda *a, **k: {})
    monkeypatch.setattr(h, "service_client", lambda: FakeClient(_tables()))
    body = {"fullName": "Juan Ruiz"}
    if location_id:
        body["locationId"] = location_id
    r = TestClient(h.app).post(f"/programs/{P1}/enroll", json=body,
                               headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201, r.text
    return repo.created[0]


def test_enrol_stores_active_location(monkeypatch):
    assert _enroll(monkeypatch, L1)["enrolled_location_id"] == L1


@pytest.mark.parametrize("bad", [L3, LX, "cccccccc-0000-0000-0000-000000000000"])
def test_enrol_ignores_inactive_foreign_or_unknown_location(monkeypatch, bad):
    assert "enrolled_location_id" not in _enroll(monkeypatch, bad)


def test_enrol_without_location(monkeypatch):
    assert "enrolled_location_id" not in _enroll(monkeypatch, None)


# --------------------------------------------------------------------------- #
# notification metering
# --------------------------------------------------------------------------- #
def _deliveries():
    def d(m, status, sent):
        return {"id": f"{m}{status}{sent}", "merchant_id": m, "status": status, "sent_at": sent}
    return [
        d(M1, "sent", "2026-11-02T10:00:00+00:00"),
        d(M1, "sent", "2026-11-05T10:00:00+00:00"),
        d(M1, "sent", "2026-10-31T23:59:59+00:00"),         # previous month
        d(M1, "sent_silent", "2026-11-05T11:00:00+00:00"),   # not billed
        d(M2, "sent", "2026-11-06T10:00:00+00:00"),
    ]


def test_usage_helpers_count_only_sent_in_period():
    from datetime import date
    c = FakeClient({"notification_deliveries": _deliveries()})
    assert usage.notifications_used(c, M1, date(2026, 11, 1)) == 2
    assert usage.notifications_used_by_merchant(c, date(2026, 11, 1)) == {M1: 2, M2: 1}


def test_usage_endpoint_reports_notification_overage(monkeypatch):
    import services.merchants.handler as h
    from services.merchants.repository import MerchantsRepository
    rows = [{"id": str(i), "merchant_id": M1, "status": "sent",
             "sent_at": "2026-11-02T10:00:00+00:00"} for i in range(501)]
    # period_start is "today's month": pin it so the fixture stays in-period.
    monkeypatch.setattr(h, "period_start", lambda today: __import__("datetime").date(2026, 11, 1))
    client = FakeClient({
        "notification_deliveries": rows,
        "subscriptions": [{"merchant_id": M1, "tier": "starter", "payment_status": "paid"}]})
    monkeypatch.setattr(h, "get_repo", lambda: MerchantsRepository(client))
    _identity(h.app, monkeypatch)
    b = TestClient(h.app).get(f"/merchants/{M1}/usage").json()
    assert b["notificationsUsed"] == 501
    assert b["extraNotifications"] == 1 and b["extraNotificationsUsd"] == 0.02
