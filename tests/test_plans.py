"""Phase 13 — pricing v2: the plan catalog, estimate math, feature gating, and the
endpoints built on them (GET /plans, GET /merchants/{id}/usage, admin stats, gated
stubs, add-operation-user limit, unlimited programs). Fake repos + monkeypatch only."""
from __future__ import annotations

from decimal import Decimal

import pytest
from conpass_common import plans
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.errors import FeatureNotInPlan
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
P1 = "33333333-3333-3333-3333-333333333333"


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tier,price,cards,extra,notifs,notif_rate,op_users", [
    ("starter", "4.99", 200, "0.044", 500, "0.015", 2),
    ("growth", "14.99", 1000, "0.034", 1000, "0.010", 5),
    ("pro", "24.99", 5000, "0.024", 10000, "0.005", None),
])
def test_catalog_values(tier, price, cards, extra, notifs, notif_rate, op_users):
    p = plans.get_plan(tier)
    assert p.price_usd == Decimal(price)
    assert p.included_cards == cards
    assert p.extra_card_usd == Decimal(extra)
    assert p.included_notifications == notifs
    assert p.extra_notification_usd == (Decimal(notif_rate) if notif_rate else None)
    assert p.operation_user_limit == op_users


def test_catalog_tiers_and_features():
    assert plans.TIER_ORDER == ("starter", "growth", "pro")
    assert set(plans.PLANS) == {"starter", "growth", "pro"}
    assert plans.PLANS["starter"].features == (
        plans.CAMPAIGNS, plans.BASIC_NOTIFICATIONS, plans.REPORTS, plans.USER_MANAGEMENT)
    for tier in plans.TIER_ORDER:       # seasonal + accumulation campaigns: every plan
        assert plans.PLANS[tier].has(plans.CAMPAIGNS)
        assert plans.PLANS[tier].has(plans.BASIC_NOTIFICATIONS)
        plans.require_feature(tier, plans.CAMPAIGNS)
    assert not plans.PLANS["starter"].has(plans.MANUAL_CAMPAIGNS)
    assert not plans.PLANS["starter"].has(plans.AUTOMATED_CAMPAIGNS)
    assert plans.PLANS["growth"].has(plans.MANUAL_CAMPAIGNS)
    assert not plans.PLANS["growth"].has(plans.AUTOMATED_CAMPAIGNS)
    assert plans.PLANS["pro"].has(plans.AUTOMATED_CAMPAIGNS)
    with pytest.raises(ValueError):
        plans.get_plan("enterprise")


# --------------------------------------------------------------------------- #
# Estimate math
# --------------------------------------------------------------------------- #
def test_estimate_boundary_and_overage():
    starter = plans.PLANS["starter"]
    at = plans.estimate(starter, 200)
    assert at.extra_cards == 0 and at.total_usd == Decimal("4.99")
    over = plans.estimate(starter, 201)
    assert over.extra_cards == 1
    assert over.extra_cards_usd == Decimal("0.04")      # 0.044 rounded at the end
    assert over.total_usd == Decimal("5.03")            # 4.99 + 0.044 = 5.034
    assert plans.estimate(starter, 250).total_usd == Decimal("7.19")  # 4.99 + 50*0.044


def test_estimate_rounds_half_up():
    # 4.99 + 0.044*? chosen so the exact total ends on a half cent: 25*0.044=1.10 (no);
    # use growth: 14.99 + 0.034*n -> n=5 => 15.16 exact; n=15 => 15.50 exact.
    # Half-up: 0.5 cents -> pro 24.99 + 0.024*? ; use money() directly for the rule.
    assert plans.money(Decimal("1.005")) == Decimal("1.01")
    assert plans.money(Decimal("1.004")) == Decimal("1.00")


def test_estimate_notifications_overage():
    growth = plans.PLANS["growth"]
    e = plans.estimate(growth, 0, 1500)
    assert e.extra_notifications == 500
    assert e.extra_notifications_usd == Decimal("5.00")
    assert e.total_usd == Decimal("19.99")
    pro = plans.estimate(plans.PLANS["pro"], 0, 10100)
    assert pro.extra_notifications_usd == Decimal("0.50")
    # Starter includes 500 and bills 0.015 beyond that.
    s = plans.estimate(plans.PLANS["starter"], 0, 500)
    assert s.extra_notifications == 0 and s.total_usd == Decimal("4.99")
    s = plans.estimate(plans.PLANS["starter"], 0, 1000)
    assert s.extra_notifications == 500
    assert s.extra_notifications_usd == Decimal("7.50")
    assert s.total_usd == Decimal("12.49")


# --------------------------------------------------------------------------- #
# require_feature / plan_to_api / helpers
# --------------------------------------------------------------------------- #
def test_require_feature():
    plans.require_feature("pro", plans.AUTOMATED_CAMPAIGNS)
    plans.require_feature("growth", plans.MANUAL_CAMPAIGNS)
    with pytest.raises(FeatureNotInPlan) as e1:
        plans.require_feature("starter", plans.MANUAL_CAMPAIGNS)
    assert e1.value.status_code == 402 and e1.value.code == "feature_not_in_plan"
    assert e1.value.details["requiredTier"] == "growth"
    with pytest.raises(FeatureNotInPlan) as e2:
        plans.require_feature("growth", plans.AUTOMATED_CAMPAIGNS)
    assert e2.value.details == {
        "feature": "automated_campaigns", "tier": "growth", "requiredTier": "pro"}


def test_plan_to_api_omits_unlimited_and_absent():
    pro = plans.plan_to_api(plans.PLANS["pro"])
    assert "operationUserLimit" not in pro
    assert pro["extraNotificationUsd"] == 0.005
    starter = plans.plan_to_api(plans.PLANS["starter"])
    assert starter["extraNotificationUsd"] == 0.015
    assert starter["includedNotifications"] == 500
    assert starter["features"][:2] == ["campaigns", "basic_notifications"]
    assert starter["operationUserLimit"] == 2
    assert starter["priceUsd"] == 4.99 and starter["extraCardUsd"] == 0.044


def test_next_tier_with_more_operation_users():
    assert plans.next_tier_with_more_operation_users("starter") == "growth"
    assert plans.next_tier_with_more_operation_users("growth") == "pro"
    assert plans.next_tier_with_more_operation_users("pro") is None


def test_subscription_to_api():
    sub = {"tier": "starter", "payment_status": "paid", "next_charge_at": "2026-09-01"}
    out = plans.subscription_to_api(sub, 10)
    assert out == {
        "tier": "starter", "paymentStatus": "paid", "priceUsd": 4.99,
        "includedCards": 200, "nextChargeAt": "2026-09-01", "lastPaymentAt": None,
        "operationUserLimit": 2, "activeCards": 10}
    assert "activeCards" not in plans.subscription_to_api(sub)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def execute(self):
        return type("R", (), {"data": self._rows, "count": len(self._rows)})()


class _Client:
    def __init__(self, tables):
        self._t = tables

    def table(self, name):
        return _Q(self._t.get(name, []))


class _MerchantsRepo:
    def __init__(self, tier="starter", active=0, programs=0, op_users=0):
        self.tier, self.active, self.programs, self.op_users = tier, active, programs, op_users

    def get_subscription(self, merchant_id):
        return {"merchant_id": merchant_id, "tier": self.tier, "payment_status": "paid"}

    def active_card_count(self, merchant_id):
        return self.active

    def count_programs(self, merchant_id):
        return self.programs

    def count_operation_users(self, merchant_id):
        return self.op_users


def _merchants(monkeypatch, **kw):
    import services.merchants.handler as h
    repo = _MerchantsRepo(**kw)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    return h


# --------------------------------------------------------------------------- #
# GET /plans (public) and GET /merchants/{id}/usage
# --------------------------------------------------------------------------- #
def test_get_plans_is_public(monkeypatch):
    h = _merchants(monkeypatch)
    resp = TestClient(h.app).get("/plans")     # no identity override, no token
    assert resp.status_code == 200
    body = resp.json()
    assert [p["tier"] for p in body] == ["starter", "growth", "pro"]
    assert body[1]["priceUsd"] == 14.99
    assert "operationUserLimit" not in body[2]


def test_usage_owner_numbers(monkeypatch):
    h = _merchants(monkeypatch, tier="starter", active=250, programs=4, op_users=1)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).get(f"/merchants/{M1}/usage")
    assert resp.status_code == 200, resp.text
    b = resp.json()
    assert b["plan"]["tier"] == "starter"
    assert b["activeCards"] == 250 and b["extraCards"] == 50
    assert b["extraCardsUsd"] == 2.2
    assert b["notificationsUsed"] == 0 and b["extraNotifications"] == 0
    assert b["extraNotificationsUsd"] == 0.0
    assert b["estimatedTotalUsd"] == 7.19
    assert b["programCount"] == 4 and b["operationUserCount"] == 1
    assert b["periodStart"].endswith("-01")


def test_usage_other_merchant_forbidden_and_admin_ok(monkeypatch):
    h = _merchants(monkeypatch)
    _identity(h.app, monkeypatch, merchant_id=M2)
    assert TestClient(h.app).get(f"/merchants/{M1}/usage").status_code == 403
    _identity(h.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    assert TestClient(h.app).get(f"/merchants/{M1}/usage").status_code == 200


def test_add_operation_user_limit_details(monkeypatch):
    h = _merchants(monkeypatch, tier="starter", op_users=2)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        f"/merchants/{M1}/operation-users",
        json={"email": "op@x.co", "station": "caja_1", "name": "Op"})
    assert resp.status_code == 402
    err = resp.json()
    assert err["code"] == "tier_limit"
    assert err["details"] == {"limit": 2, "tier": "starter", "requiredTier": "growth"}


def test_add_operation_user_pro_is_unlimited(monkeypatch):
    h = _merchants(monkeypatch, tier="pro", op_users=500)
    monkeypatch.setattr(h, "provision_user", lambda **kw: type(
        "U", (), {"user_id": U1, "email": kw["email"], "temp_password": "x"})())
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        f"/merchants/{M1}/operation-users",
        json={"email": "op@x.co", "station": "caja_1", "name": "Op"})
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Admin stats with overage
# --------------------------------------------------------------------------- #
def test_admin_stats_tier_counts_and_mrr(monkeypatch):
    from services.admin import handler as h

    tables = {
        "subscriptions": [
            {"merchant_id": M1, "tier": "starter", "payment_status": "paid"},
            {"merchant_id": M2, "tier": "pro", "payment_status": "paid"},
            {"merchant_id": "m3", "tier": "growth", "payment_status": "overdue"},
        ],
        "merchant_active_passes": [
            {"merchant_id": M1, "active_pass_count": 250},    # +50 * 0.044 = 2.20
            {"merchant_id": "m3", "active_pass_count": 5000},  # unpaid -> excluded
        ],
    }
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_Client(tables)))
    _identity(h.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    body = TestClient(h.app).get("/admin/stats").json()
    assert body["activeClients"] == 2
    assert body["subscriptionMrrUsd"] == pytest.approx(29.98)
    assert body["mrrUsd"] == pytest.approx(32.18)
    assert body["tierCounts"] == {"starter": 1, "growth": 1, "pro": 1}
    assert body["overdueCount"] == 1


# --------------------------------------------------------------------------- #
# Feature-gated stubs
# --------------------------------------------------------------------------- #
def _gate(monkeypatch, module, tier):
    tables = {"subscriptions": [{"merchant_id": M1, "tier": tier}]}
    monkeypatch.setattr(module, "service_client", lambda: _Client(tables))
    _identity(module.app, monkeypatch)


def test_notifications_gating(monkeypatch):
    from services.notifications import handler as h

    _gate(monkeypatch, h, "starter")
    c = TestClient(h.app)
    r = c.post("/notifications/reminders", json={"programId": P1})
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    assert r.json()["details"]["requiredTier"] == "growth"

    _gate(monkeypatch, h, "growth")
    assert c.post("/notifications/reminders", json={"programId": P1}).status_code == 501
    r = c.post("/notifications/reminders", json={"programId": P1, "segment": "birthday"})
    assert r.status_code == 402 and r.json()["details"]["requiredTier"] == "pro"

    _gate(monkeypatch, h, "pro")
    r = c.post("/notifications/reminders", json={"programId": P1, "segment": "birthday"})
    assert r.status_code == 501


def test_birthday_gating(monkeypatch):
    from services.birthday import handler as h

    auto = {"enabled": False, "benefitType": "discount"}
    card = {"programId": P1, "customerId": P1}
    for tier, expected in (("starter", 402), ("growth", 402), ("pro", 501)):
        _gate(monkeypatch, h, tier)
        c = TestClient(h.app)
        r = c.put(f"/programs/{P1}/birthday-automation", json=auto)
        assert r.status_code == expected, (tier, r.text)
        r2 = c.post("/birthday-cards", json=card)
        assert r2.status_code == expected, (tier, r2.text)
        if expected == 402:
            assert r.json()["code"] == "feature_not_in_plan"


# --------------------------------------------------------------------------- #
# Programs: no limit
# --------------------------------------------------------------------------- #
def test_program_create_has_no_limit(monkeypatch):
    import services.programs.handler as h

    class _Repo:
        def count_programs(self, merchant_id):
            return 50

        def create(self, data):
            return {**data, "id": P1, "created_at": "2026-08-13T00:00:00Z"}

    monkeypatch.setattr(h, "get_repo", lambda: _Repo())
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post("/programs", json={
        "type": "loyalty_stamps", "name": "Café", "mechanic": "stamps",
        "stampsForReward": 8, "reward": "Free coffee",
        "appearance": {}, "wallets": ["google"]})
    assert resp.status_code == 201, resp.text
