"""Pricing (business model v3.0, Phase 18; first built Phase 13): the plan catalog,
estimate math, feature gating, and the endpoints built on them (GET /plans,
GET /merchants/{id}/usage, admin stats, gated stubs, add-operation-user limit,
unlimited programs). Fake repos + monkeypatch only."""
from __future__ import annotations

from datetime import date
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
# Catalog — business model v3.0 (Phase 18, D21)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "tier,launch,listp,a_launch,a_list,cards,extra,notifs,notif_rate,branches", [
    ("starter", "5.99", "7.99", "56.99", "75.99", 200, "0.044", 500, "0.015", 1),
    ("growth", "17.99", "22.99", "171.99", "219.99", 1000, "0.034", 3000, "0.010", 3),
    ("pro", "57.99", "74.99", "555.99", "718.99", 5000, "0.024", 10000, "0.005", 10),
])
def test_catalog_values(tier, launch, listp, a_launch, a_list, cards, extra, notifs,
                        notif_rate, branches):
    p = plans.get_plan(tier)
    assert p.monthly_usd(plans.LAUNCH) == Decimal(launch) == p.price_usd
    assert p.monthly_usd(plans.LIST) == Decimal(listp)
    assert p.annual_usd(plans.LAUNCH) == Decimal(a_launch)
    assert p.annual_usd(plans.LIST) == Decimal(a_list)
    assert p.included_cards == cards
    assert p.extra_card_usd == Decimal(extra)
    assert p.included_notifications == notifs
    assert p.extra_notification_usd == Decimal(notif_rate)
    assert p.location_limit == branches
    assert p.operation_user_limit is None            # cashiers unlimited on every plan
    # Annual always saves at least 20 % against 12 months, in both phases.
    for phase in plans.PRICING_PHASES:
        assert p.annual_usd(phase) <= p.monthly_usd(phase) * 12 * Decimal("0.8")


def test_catalog_tiers_and_features():
    assert plans.TIER_ORDER == ("starter", "growth", "pro")
    assert set(plans.PLANS) == {"starter", "growth", "pro"}
    assert plans.PLANS["starter"].features == (
        plans.CAMPAIGNS, plans.BASIC_NOTIFICATIONS, plans.REPORTS, plans.USER_MANAGEMENT,
        plans.NEARBY_NOTIFICATIONS)
    for tier in plans.TIER_ORDER:       # every plan: campaigns, notices, "Estás cerca"
        for f in (plans.CAMPAIGNS, plans.BASIC_NOTIFICATIONS, plans.NEARBY_NOTIFICATIONS):
            assert plans.PLANS[tier].has(f)
    assert not plans.PLANS["starter"].has(plans.MANUAL_CAMPAIGNS)
    assert plans.PLANS["growth"].has(plans.MANUAL_CAMPAIGNS)
    for f in (plans.AUTOMATED_CAMPAIGNS, plans.BRANCH_REPORTS, plans.STAFF_ROLES,
              plans.DATA_EXPORT):
        assert plans.cheapest_with(f) == "pro"
    with pytest.raises(ValueError):
        plans.get_plan("enterprise")


# --------------------------------------------------------------------------- #
# Estimate math — the worked examples of modelo-negocio v3.0
# --------------------------------------------------------------------------- #
def test_split_iva_matches_invoice_example():
    assert plans.split_iva(Decimal("5.99")) == (Decimal("5.21"), Decimal("0.78"))
    assert plans.split_iva(Decimal("7.99")) == (Decimal("6.95"), Decimal("1.04"))


def test_estimate_boundary_and_overage():
    starter = plans.PLANS["starter"]
    at = plans.estimate(starter, 200)
    assert at.extra_cards == 0 and at.total_usd == Decimal("5.99")
    assert at.subscription_net_usd == Decimal("5.21") and at.iva_usd == Decimal("0.78")
    over = plans.estimate(starter, 201)
    assert over.extra_cards == 1 and over.extra_cards_usd == Decimal("0.04")
    assert over.total_usd == Decimal("6.04")            # 5.99 + 0.044 * 1.15 = 6.0406
    # Decision 3: 400 enrolled, 280 paused → would be 16.11 without the 90-day rule.
    assert plans.estimate(starter, 400).total_usd == Decimal("16.11")
    assert plans.estimate(starter, 120).total_usd == Decimal("5.99")


def test_estimate_rounds_half_up():
    assert plans.money(Decimal("1.005")) == Decimal("1.01")
    assert plans.money(Decimal("1.004")) == Decimal("1.00")


def test_estimate_notifications_overage():
    # Decision 2 table (balance notice on): 10.30 / 29.49 / 69.49.
    assert plans.estimate(plans.PLANS["starter"], 150, 750).total_usd == Decimal("10.30")
    assert plans.estimate(plans.PLANS["growth"], 800, 4000).total_usd == Decimal("29.49")
    assert plans.estimate(plans.PLANS["pro"], 3000, 12000).total_usd == Decimal("69.49")
    # Decision 10: a 6,000-card chain on Pro pays 85.59.
    assert plans.estimate(plans.PLANS["pro"], 6000, 0).total_usd == Decimal("85.59")
    e = plans.estimate(plans.PLANS["growth"], 0, 3000)
    assert e.extra_notifications == 0 and e.total_usd == Decimal("17.99")


def test_estimate_phase_and_annual_month():
    starter = plans.PLANS["starter"]
    assert plans.estimate(starter, 0, phase=plans.LIST).total_usd == Decimal("7.99")
    # An annual term's non-renewal month bills only the overage (+ IVA).
    e = plans.estimate(starter, 210, subscription_usd=Decimal("0"))
    assert e.subscription_usd == Decimal("0.00")
    assert e.total_usd == Decimal("0.51")               # 10 * 0.044 * 1.15 = 0.506


def test_subscription_price_lock():
    sub = {"tier": "growth", "billing_period": "annual", "locked_price_usd": "171.99"}
    assert plans.subscription_price_usd(sub, plans.LIST) == Decimal("171.99")
    sub_unlocked = {"tier": "growth", "billing_period": "annual", "locked_price_usd": None}
    assert plans.subscription_price_usd(sub_unlocked, plans.LIST) == Decimal("219.99")
    assert plans.subscription_price_usd({"tier": "growth"}, plans.LAUNCH) == Decimal("17.99")


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


def test_plan_to_api_launch_and_list():
    starter = plans.plan_to_api(plans.PLANS["starter"])
    assert starter["priceUsd"] == 5.99 and starter["listPriceUsd"] == 7.99
    assert starter["annualPriceUsd"] == 56.99 and starter["annualListPriceUsd"] == 75.99
    assert starter["pricingPhase"] == "launch" and starter["locationLimit"] == 1
    assert starter["includedNotifications"] == 500 and starter["extraNotificationUsd"] == 0.015
    assert "operationUserLimit" not in starter
    listed = plans.plan_to_api(plans.PLANS["pro"], plans.LIST)
    assert listed["priceUsd"] == listed["listPriceUsd"] == 74.99


def test_next_tier_helpers():
    for tier in plans.TIER_ORDER:
        assert plans.next_tier_with_more_operation_users(tier) is None
    assert plans.next_tier_with_more_locations("starter") == "growth"
    assert plans.next_tier_with_more_locations("growth") == "pro"
    assert plans.next_tier_with_more_locations("pro") is None


def test_subscription_to_api():
    sub = {"tier": "starter", "payment_status": "paid", "next_charge_at": "2026-09-01"}
    out = plans.subscription_to_api(sub, 10)
    assert out == {
        "tier": "starter", "paymentStatus": "paid", "billingPeriod": "monthly",
        "priceUsd": 5.99, "listPriceUsd": 7.99, "priceLocked": False,
        "includedCards": 200, "includedNotifications": 500, "locationLimit": 1,
        "nextChargeAt": "2026-09-01", "lastPaymentAt": None, "activeCards": 10}
    assert "activeCards" not in plans.subscription_to_api(sub)
    annual = plans.subscription_to_api(
        {"tier": "pro", "payment_status": "paid", "billing_period": "annual",
         "locked_price_usd": 555.99}, phase=plans.LIST)
    assert annual["priceUsd"] == 555.99 and annual["listPriceUsd"] == 718.99
    assert annual["priceLocked"] is True


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

    def gte(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) is not None and r[col] >= val]
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, lo, hi):
        self._rows = self._rows[lo:hi + 1]
        return self

    def execute(self):
        return type("R", (), {"data": self._rows, "count": len(self._rows)})()


class _Client:
    def __init__(self, tables):
        self._t = tables

    def table(self, name):
        return _Q(self._t.get(name, []))


def _merchants(monkeypatch, tier="starter", active=0, programs=0, op_users=0, notifications=0):
    """The merchants handler over an in-memory client seeded with the usage inputs."""
    from _p18_fake import FakeData

    import services.merchants.handler as h
    from services.merchants.repository import MerchantsRepository

    data = FakeData(
        subscriptions=[{"merchant_id": M1, "tier": tier, "payment_status": "paid"}],
        merchant_active_passes=[{"merchant_id": M1, "active_pass_count": active}],
        programs=[{"id": f"p{i}", "merchant_id": M1} for i in range(programs)],
        profiles=[{"user_id": f"u{i}", "merchant_id": M1, "role": "operation_user"}
                  for i in range(op_users)],
        notification_deliveries=[{"id": f"n{i}", "merchant_id": M1, "status": "sent",
                                  "sent_at": f"{date.today():%Y-%m}-01T00:00:00+00:00"}
                                 for i in range(notifications)])
    repo = MerchantsRepository(data)
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
    assert body[1]["priceUsd"] == 17.99 and body[1]["listPriceUsd"] == 22.99
    assert body[1]["annualPriceUsd"] == 171.99 and body[1]["pricingPhase"] == "launch"
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
    assert b["subscriptionUsd"] == 5.99 and b["billingPeriod"] == "monthly"
    assert b["estimatedTotalUsd"] == 8.52           # 5.99 + 2.20 + 15 % IVA on the 2.20
    assert b["ivaUsd"] == pytest.approx(0.78 + 0.33)
    assert b["programCount"] == 4 and b["operationUserCount"] == 1 and b["locationCount"] == 0
    assert b["periodStart"].endswith("-01")


def test_usage_other_merchant_forbidden_and_admin_ok(monkeypatch):
    h = _merchants(monkeypatch)
    _identity(h.app, monkeypatch, merchant_id=M2)
    assert TestClient(h.app).get(f"/merchants/{M1}/usage").status_code == 403
    _identity(h.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    assert TestClient(h.app).get(f"/merchants/{M1}/usage").status_code == 200


@pytest.mark.parametrize("tier,existing", [("starter", 2), ("starter", 30), ("growth", 5),
                                           ("pro", 500)])
def test_add_operation_user_is_unlimited_on_every_plan(monkeypatch, tier, existing):
    """v3.0: cashiers are unlimited, so the old 402 tier_limit (2 / 5) is gone."""
    h = _merchants(monkeypatch, tier=tier, op_users=existing)
    monkeypatch.setattr(h, "provision_user", lambda **kw: type(
        "U", (), {"user_id": U1, "email": kw["email"], "temp_password": "x"})())
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        f"/merchants/{M1}/operation-users",
        json={"email": "op@x.co", "station": "caja_1", "name": "Op"})
    assert resp.status_code == 201, resp.text


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
            {"merchant_id": M1, "active_pass_count": 250},    # +50 * 0.044 = 2.20 (+ IVA)
            {"merchant_id": "m3", "active_pass_count": 5000},  # unpaid -> excluded
        ],
    }
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(_Client(tables)))
    _identity(h.app, monkeypatch, roles=("platform_admin",), merchant_id=None)
    body = TestClient(h.app).get("/admin/stats").json()
    assert body["activeClients"] == 2
    assert body["subscriptionMrrUsd"] == pytest.approx(5.99 + 57.99)
    assert body["mrrUsd"] == pytest.approx(8.52 + 57.99)
    assert body["tierCounts"] == {"starter": 1, "growth": 1, "pro": 1}
    assert body["overdueCount"] == 1
    assert body["pendingVerificationCount"] == 0 and body["suspendedCount"] == 0


# --------------------------------------------------------------------------- #
# Feature-gated stubs
# --------------------------------------------------------------------------- #
def _gate(monkeypatch, module, tier):
    tables = {"subscriptions": [{"merchant_id": M1, "tier": tier}]}
    monkeypatch.setattr(module, "service_client", lambda: _Client(tables))
    _identity(module.app, monkeypatch)


def test_notifications_gating(monkeypatch):
    """Manual sends: general->all is every plan; segments/event/flash/link are Growth+."""
    from services.notifications import handler as h

    _gate(monkeypatch, h, "starter")
    c = TestClient(h.app)
    r = c.post("/notifications", json={"type": "flash", "audience": "all", "body": "x",
                                       "flashHours": 2})
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    assert r.json()["details"]["requiredTier"] == "growth"
    r = c.post("/notifications", json={"type": "general", "audience": "near", "body": "x"})
    assert r.status_code == 402


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
        def is_demo(self, merchant_id):
            return False

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
