"""Phase 16 — notifications HTTP service, notifier Lambda routing, operations hooks."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.errors import ValidationFailed
from conpass_common.idempotency import InMemoryIdempotencyStore
from fastapi.testclient import TestClient

from tests.fakedb import FakeDB

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "22222222-2222-2222-2222-222222222299"
P1 = "33333333-3333-3333-3333-333333333333"
CARD1 = "44444444-4444-4444-4444-444444444444"
OP1 = "55555555-5555-5555-5555-555555555555"
L1 = "66666666-6666-6666-6666-666666666666"
L2 = "66666666-6666-6666-6666-666666666667"


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


def _svc(monkeypatch, tier="growth", **tables):
    from services.notifications import handler as h

    db = FakeDB(subscriptions=[{"merchant_id": M1, "tier": tier}],
                merchant_locations=[{"id": L1, "merchant_id": M1, "name": "Centro",
                                     "active": True, "created_at": "1"},
                                    {"id": L2, "merchant_id": M2, "name": "Otro",
                                     "active": True, "created_at": "2"}],
                **tables)
    db.rpcs["notification_audience"] = lambda p: [{"card_id": str(i)} for i in range(
        {"all": 5, "near": 2, "inactive": 1, "level": 3, "location": 4}[p["p_audience"]])]
    db.rpcs["enqueue_manual_notification"] = lambda p: _enqueue(db, p)
    monkeypatch.setattr(h, "service_client", lambda: db)
    _identity(h.app, monkeypatch)
    return TestClient(h.app), db


def _enqueue(db, params):
    n = next(r for r in db.rows("notifications") if r["id"] == params["p_notification"])
    n["recipients"], n["status"] = 5, "queued"
    return 5


def _auto(**over):
    base = {"automations": {
        "balance": {"enabled": True}, "ready": {"enabled": True},
        "near": {"enabled": True, "stamps": 2, "points": 10},
        "milestone": {"enabled": True, "every": 10},
        "expiry": {"enabled": False, "warnDays": 7},
        "inactive": {"enabled": False, "days": 28}},
        "balanceExpiryDays": None, "cardLocations": {"enrolled": True, "topVisited": False}}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_settings_defaults_then_roundtrip(monkeypatch):
    c, db = _svc(monkeypatch)
    r = c.get("/notifications/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["automations"]["inactive"]["enabled"] is False
    assert body["automations"]["expiry"] == {"enabled": True, "warnDays": 7}
    assert body["balanceExpiryDays"] is None
    assert body["platformRules"]["quietStart"] == "21:00"
    put = c.put("/notifications/settings", json=_auto(balanceExpiryDays=60))
    assert put.status_code == 200, put.text
    assert put.json()["automations"]["near"]["stamps"] == 2
    assert c.get("/notifications/settings").json()["balanceExpiryDays"] == 60
    assert db.rows("merchant_notification_settings")[0]["near_stamps"] == 2


def test_settings_inactive_needs_pro(monkeypatch):
    c, _ = _svc(monkeypatch, tier="growth")
    auto = _auto()
    auto["automations"]["inactive"]["enabled"] = True
    r = c.put("/notifications/settings", json=auto)
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    assert r.json()["details"]["requiredTier"] == "pro"
    c, _ = _svc(monkeypatch, tier="pro")
    assert c.put("/notifications/settings", json=auto).status_code == 200


def test_settings_expiry_validation(monkeypatch):
    c, _ = _svc(monkeypatch)
    on = _auto()
    on["automations"]["expiry"]["enabled"] = True
    assert c.put("/notifications/settings", json=on).status_code == 200    # inert default
    bad = _auto(balanceExpiryDays=7)                                      # warn 7 >= 7
    assert c.put("/notifications/settings", json=bad).status_code == 422
    ok = _auto(balanceExpiryDays=30)
    ok["automations"]["expiry"]["enabled"] = True
    assert c.put("/notifications/settings", json=ok).status_code == 200


def test_owner_only_and_demo_owner_allowed(monkeypatch):
    from services.notifications import handler as h
    c, _ = _svc(monkeypatch)
    _identity(h.app, monkeypatch, roles=("operation_user",))
    assert c.get("/notifications/settings").status_code == 403
    _identity(h.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    assert c.get("/notifications").status_code == 403


# --------------------------------------------------------------------------- #
# Audiences
# --------------------------------------------------------------------------- #
def test_audiences_counts(monkeypatch):
    c, _ = _svc(monkeypatch)
    body = c.get("/notifications/audiences").json()
    assert (body["all"], body["near"], body["inactive"]) == (5, 2, 1)
    assert [x["levelMin"] for x in body["levels"]] == list(range(2, 11))
    assert body["levels"][0]["count"] == 3
    assert body["locations"] == [{"count": 4, "locationId": L1, "name": "Centro"}]


# --------------------------------------------------------------------------- #
# Create / list / cancel
# --------------------------------------------------------------------------- #
GENERAL = {"type": "general", "audience": "all", "body": "Hola"}


def _future(days=1):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def test_starter_general_to_all_allowed_and_queued(monkeypatch):
    c, db = _svc(monkeypatch, tier="starter")
    r = c.post("/notifications", json=GENERAL)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued" and r.json()["recipients"] == 5
    assert ("enqueue_manual_notification", {"p_notification": r.json()["id"]}) in db.rpc_calls
    assert db.rows("notifications")[0]["created_by"] == U1


@pytest.mark.parametrize("extra", [
    {"type": "event", "eventAt": "2030-01-01T00:00:00Z"},
    {"type": "flash", "flashHours": 4},
    {"audience": "near"},
    {"url": "https://x.co/y"},
])
def test_starter_gating_402(monkeypatch, extra):
    c, db = _svc(monkeypatch, tier="starter")
    r = c.post("/notifications", json={**GENERAL, **extra})
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    assert r.json()["details"]["requiredTier"] == "growth"
    assert db.rows("notifications") == []


@pytest.mark.parametrize("payload", [
    {"audience": "level"},                                   # levelMin missing
    {"audience": "location"},                                # locationId missing
    {"audience": "location", "locationId": L2},              # foreign locale
    {"type": "event"},                                       # eventAt missing
    {"type": "flash"},                                       # flashHours missing
    {"scheduledAt": "2020-01-01T00:00:00Z"},                 # past
    {"scheduledAt": "2999-01-01T00:00:00Z"},                 # > 60 days
])
def test_growth_validation_422(monkeypatch, payload):
    c, db = _svc(monkeypatch, tier="growth")
    r = c.post("/notifications", json={**GENERAL, **payload})
    assert r.status_code == 422, r.text
    assert db.rows("notifications") == []


def test_growth_segment_event_and_schedule(monkeypatch):
    c, db = _svc(monkeypatch, tier="growth")
    r = c.post("/notifications", json={"type": "event", "audience": "location",
                                       "locationId": L1, "body": "Cata",
                                       "eventAt": _future(3)})
    assert r.status_code == 202 and r.json()["locationName"] == "Centro"
    s = c.post("/notifications", json={**GENERAL, "scheduledAt": _future(2)})
    assert s.status_code == 202 and s.json()["status"] == "scheduled"
    assert len([x for x in db.rpc_calls if x[0] == "enqueue_manual_notification"]) == 1


def test_enqueue_failure_cancels_and_raises(monkeypatch):
    c, db = _svc(monkeypatch)

    def boom(p):
        raise RuntimeError("rpc down")
    db.rpcs["enqueue_manual_notification"] = boom
    with pytest.raises(RuntimeError):
        c.post("/notifications", json=GENERAL)
    assert db.rows("notifications")[0]["status"] == "cancelled"


def test_history_newest_first_with_limit(monkeypatch):
    rows = [{"id": str(uuid.uuid4()), "merchant_id": M1, "source": "manual", "type": "general",
             "audience": "all", "body": f"b{i}", "status": "sent", "recipients": 2,
             "created_at": f"2026-11-0{i}T00:00:00+00:00"} for i in range(1, 5)]
    rows.append({**rows[0], "id": str(uuid.uuid4()), "merchant_id": M2,
                 "created_at": "2026-11-09T00:00:00+00:00"})
    c, _ = _svc(monkeypatch, notifications=rows)
    r = c.get("/notifications?limit=3")
    assert [x["body"] for x in r.json()] == ["b4", "b3", "b2"]
    assert r.json()[0]["recipients"] == 2 and r.json()[0]["visits48h"] == 0


def test_cancel_scheduled_only(monkeypatch):
    ids = {k: str(uuid.uuid4()) for k in ("sched", "queued", "foreign")}
    base = {"source": "manual", "type": "general", "audience": "all", "body": "b"}
    c, db = _svc(monkeypatch, notifications=[
        {"id": ids["sched"], "merchant_id": M1, "status": "scheduled", **base},
        {"id": ids["queued"], "merchant_id": M1, "status": "queued", **base},
        {"id": ids["foreign"], "merchant_id": M2, "status": "scheduled", **base}])
    assert c.delete(f"/notifications/{ids['queued']}").status_code == 409
    assert c.delete(f"/notifications/{ids['foreign']}").status_code == 404
    assert c.delete(f"/notifications/{uuid.uuid4()}").status_code == 404
    assert c.delete(f"/notifications/{ids['sched']}").status_code == 204
    assert [r["status"] for r in db.rows("notifications") if r["id"] == ids["sched"]] == [
        "cancelled"]
    assert c.delete(f"/notifications/{ids['sched']}").status_code == 409


# --------------------------------------------------------------------------- #
# Notifier Lambda routing
# --------------------------------------------------------------------------- #
def test_notifier_routes_jobs(monkeypatch):
    from services.notifier import handler as n

    calls = []
    monkeypatch.setattr(n, "service_client", lambda: "db")
    monkeypatch.setattr(n.dispatch, "run_dispatch", lambda c: calls.append("dispatch") or {})
    monkeypatch.setattr(n.automations, "run_daily", lambda c: calls.append("daily") or {})
    monkeypatch.setattr(n.demo, "expire_demo_cards", lambda c: calls.append("demo") or {})
    n.handler({"job": "dispatch"}, None)
    assert calls == ["dispatch"]
    calls.clear()
    n.handler({"job": "daily"}, None)
    assert calls == ["demo", "daily", "dispatch"]
    with pytest.raises(ValueError):
        n.handler({"job": "bogus"}, None)


# --------------------------------------------------------------------------- #
# Operations hooks
# --------------------------------------------------------------------------- #
def _ops(monkeypatch):
    from services.operations import handler as h
    from services.operations.logic import CardState, ProgramRules
    from services.operations.repository import CardRow, InMemoryRepository

    repo = InMemoryRepository()
    repo.rules[P1] = ProgramRules("loyalty_stamps", "stamps", 8, None, None)
    repo.cards[CARD1] = CardRow(CARD1, P1, M1, "loyalty_stamps",
                                CardState(stamps=6, points=0, rewards_available=0, active=True))
    store = InMemoryIdempotencyStore()
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: store)
    monkeypatch.setattr(h, "service_client", lambda: "db")
    monkeypatch.setattr(h, "_push_wallet_update", lambda *a: None)
    _identity(h.app, monkeypatch, roles=("operation_user",))
    return h, TestClient(h.app), repo


def _accrue(c, **extra):
    return c.post("/operations/accrue", json={"cardId": CARD1, "operationUserId": OP1,
                                              "stamps": 1, **extra},
                  headers={"Idempotency-Key": str(uuid.uuid4())})


def test_accrue_persists_resolved_location(monkeypatch):
    h, c, repo = _ops(monkeypatch)
    seen = []
    monkeypatch.setattr(h.locations, "resolve_scan_location",
                        lambda client, ident, merchant, loc: seen.append((merchant, loc)) or loc)
    monkeypatch.setattr(h, "after_accrual", lambda *a, **k: None)
    r = _accrue(c, locationId=L1)
    assert r.status_code == 200, r.text
    assert seen == [(M1, L1)]
    assert repo.txns[-1].location_id == L1
    assert r.json()["transaction"]["locationId"] == L1


def test_accrue_without_location_skips_resolution(monkeypatch):
    h, c, repo = _ops(monkeypatch)

    def nope(*a):
        raise AssertionError("must not resolve")
    monkeypatch.setattr(h.locations, "resolve_scan_location", nope)
    monkeypatch.setattr(h, "after_accrual", lambda *a, **k: None)
    assert _accrue(c).status_code == 200
    assert repo.txns[-1].location_id is None


def test_bad_location_is_422_and_nothing_commits(monkeypatch):
    h, c, repo = _ops(monkeypatch)

    def reject(*a):
        raise ValidationFailed("locale not allowed")
    monkeypatch.setattr(h.locations, "resolve_scan_location", reject)
    assert _accrue(c, locationId=L1).status_code == 422
    assert repo.txns == []
    r = c.post("/operations/redeem", json={"cardId": CARD1, "operationUserId": OP1,
                                           "locationId": L1},
               headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 422


def test_redeem_persists_location(monkeypatch):
    h, c, repo = _ops(monkeypatch)
    repo.cards[CARD1].state = type(repo.cards[CARD1].state)(
        stamps=0, points=0, rewards_available=1, active=True)
    monkeypatch.setattr(h.locations, "resolve_scan_location", lambda *a: a[-1])
    r = c.post("/operations/redeem", json={"cardId": CARD1, "operationUserId": OP1,
                                           "locationId": L1},
               headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 200 and repo.txns[-1].location_id == L1


def test_accrue_feeds_automation_with_before_after(monkeypatch):
    h, c, repo = _ops(monkeypatch)
    events = []
    monkeypatch.setattr(h, "after_accrual", lambda client, e: events.append(e))
    assert _accrue(c, stamps=2).status_code == 200      # 6 -> 8 completes the reward
    e = events[0]
    assert (e.before_held, e.after_held, e.gained, e.target, e.rewards_earned,
            e.mechanic) == (6, 0, 2, 8, 1, "stamps")


def test_automation_failure_never_fails_accrue(monkeypatch):
    h, c, repo = _ops(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("outbox down")
    monkeypatch.setattr(h, "after_accrual", boom)
    r = _accrue(c)
    assert r.status_code == 200 and len(repo.txns) == 1
