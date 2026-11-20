"""Phase 18 (B3) — conpass_common.billing: dates, direction rules, the month's estimate, usage,
the activity log and the notifier's scheduled plan-change job."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from _p18_fake import FakeData
from conpass_common import billing
from conpass_common.errors import Conflict, NotFound
from conpass_common.plans import PLANS

M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
TODAY = date(2026, 11, 19)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("start,period,expected", [
    (date(2026, 11, 19), "monthly", date(2026, 12, 19)),
    (date(2026, 12, 15), "monthly", date(2027, 1, 15)),
    (date(2026, 1, 31), "monthly", date(2026, 2, 28)),         # day clamps
    (date(2026, 11, 19), "annual", date(2027, 11, 19)),
    (date(2028, 2, 29), "annual", date(2029, 2, 28)),          # leap day clamps
])
def test_add_period(start, period, expected):
    assert billing.add_period(start, period) == expected


def test_first_of_next_month():
    assert billing.first_of_next_month(date(2026, 11, 19)) == date(2026, 12, 1)
    assert billing.first_of_next_month(date(2026, 12, 31)) == date(2027, 1, 1)


# --------------------------------------------------------------------------- #
# Direction of a plan change
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("frm,to,expected", [
    (("starter", "monthly"), ("growth", "monthly"), "upgrade"),
    (("growth", "monthly"), ("starter", "monthly"), "downgrade"),
    # the tier dominates the period
    (("growth", "annual"), ("pro", "monthly"), "upgrade"),
    (("pro", "monthly"), ("growth", "annual"), "downgrade"),
    # same tier: monthly -> annual is an upgrade, annual -> monthly a downgrade
    (("growth", "monthly"), ("growth", "annual"), "upgrade"),
    (("growth", "annual"), ("growth", "monthly"), "downgrade"),
    (("growth", "monthly"), ("growth", "monthly"), None),
    (("pro", "annual"), ("pro", "annual"), None),
])
def test_plan_change_direction(frm, to, expected):
    assert billing.plan_change_direction(*frm, *to) == expected


# --------------------------------------------------------------------------- #
# The month's estimate
# --------------------------------------------------------------------------- #
def _sub(**kw):
    return {"merchant_id": M1, "tier": "starter", "payment_status": "paid", **kw}


def test_month_estimate_monthly_charges_the_subscription_every_month():
    est = billing.month_estimate(_sub(), 150, 150, "launch", TODAY)
    assert est.total_usd == Decimal("5.99")
    assert est.iva_usd == Decimal("0.78")


def test_month_estimate_monthly_with_overage_matches_the_model_example():
    # model v3.0: Starter with 750 notifications -> $10.30
    est = billing.month_estimate(_sub(), 150, 750, "launch", TODAY)
    assert est.total_usd == Decimal("10.30")


def test_month_estimate_annual_only_bills_the_subscription_in_the_renewal_month():
    annual = _sub(billing_period="annual", locked_price_usd=56.99)
    # next charge next year: only the overage
    far = billing.month_estimate({**annual, "next_charge_at": "2027-06-01"}, 250, 0,
                                 "launch", TODAY)
    assert far.subscription_usd == Decimal("0.00")
    assert far.total_usd == Decimal("2.53")          # 50 * 0.044 = 2.20 + 15 % IVA
    # next charge this month: the locked price
    due = billing.month_estimate({**annual, "next_charge_at": "2026-11-28"}, 0, 0,
                                 "launch", TODAY)
    assert due.subscription_usd == Decimal("56.99")
    # a renewal already past due is still owed
    late = billing.month_estimate({**annual, "next_charge_at": "2026-10-28"}, 0, 0,
                                  "launch", TODAY)
    assert late.subscription_usd == Decimal("56.99")


def test_month_estimate_annual_locked_price_survives_the_list_phase():
    annual = _sub(billing_period="annual", locked_price_usd=56.99, next_charge_at="2026-11-28")
    est = billing.month_estimate(annual, 0, 0, "list", TODAY)
    assert est.subscription_usd == Decimal("56.99")     # not 75.99


# --------------------------------------------------------------------------- #
# usage_for
# --------------------------------------------------------------------------- #
def _data():
    return FakeData(
        subscriptions=[_sub(billing_period="monthly")],
        merchant_active_passes=[{"merchant_id": M1, "active_pass_count": 250}],
        merchant_locations=[{"id": "l1", "merchant_id": M1, "active": True},
                            {"id": "l2", "merchant_id": M1, "active": False}],
        programs=[{"id": "p1", "merchant_id": M1}],
        profiles=[{"user_id": "u1", "merchant_id": M1, "role": "operation_user"},
                  {"user_id": "u2", "merchant_id": M1, "role": "merchant_owner"}],
    )


def test_usage_for_counts_everything(monkeypatch):
    monkeypatch.setattr(billing, "today_utc", lambda: TODAY)
    out = billing.usage_for(_data(), M1)
    assert out["plan"]["tier"] == "starter" and out["plan"]["pricingPhase"] == "launch"
    assert out["activeCards"] == 250 and out["extraCards"] == 50
    assert out["extraCardsUsd"] == 2.2
    assert out["subscriptionUsd"] == 5.99
    assert out["estimatedTotalUsd"] == 8.52              # 5.99 + 2.20 + 0.33 IVA
    assert out["ivaUsd"] == pytest.approx(0.78 + 0.33)
    assert out["billingPeriod"] == "monthly"
    assert out["locationCount"] == 1                      # only the active branch
    assert out["programCount"] == 1 and out["operationUserCount"] == 1
    assert out["periodStart"] == "2026-11-01"


def test_usage_for_list_phase_prices_the_subscription_at_list(monkeypatch):
    monkeypatch.setattr(billing, "today_utc", lambda: TODAY)
    data = _data()
    data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    out = billing.usage_for(data, M1)
    assert out["subscriptionUsd"] == 7.99 and out["plan"]["priceUsd"] == 7.99


def test_usage_for_unknown_merchant_is_404():
    with pytest.raises(NotFound):
        billing.usage_for(FakeData(), M1)


# --------------------------------------------------------------------------- #
# Activity log
# --------------------------------------------------------------------------- #
def test_log_activity_writes_a_row():
    data = FakeData()
    billing.log_activity(data, M1, "activated", actor_user_id="u1", actor_role="platform_admin",
                         details={"tier": "starter"})
    row = data.tables["merchant_activity"][0]
    assert row["action"] == "activated" and row["actor_user_id"] == "u1"
    assert row["details"] == {"tier": "starter"}


def test_log_activity_never_raises():
    data = FakeData()
    data.fail_inserts.add("merchant_activity")
    billing.log_activity(data, M1, "activated", actor_role="system")     # no exception


# --------------------------------------------------------------------------- #
# apply_scheduled_plan_changes (notifier)
# --------------------------------------------------------------------------- #
def _scheduled(**kw):
    return {"id": "r1", "merchant_id": M1, "from_tier": "pro", "to_tier": "growth",
            "from_period": "monthly", "to_period": "monthly", "direction": "downgrade",
            "status": "scheduled", "effective_at": "2026-11-19",
            "created_at": "2026-11-01T00:00:00+00:00", **kw}


def test_scheduled_downgrade_is_applied_once():
    data = FakeData(subscriptions=[_sub(tier="pro", billing_period="monthly")],
                    plan_change_requests=[_scheduled()])
    assert billing.apply_scheduled_plan_changes(data, TODAY) == 1
    sub = data.tables["subscriptions"][0]
    assert sub["tier"] == "growth" and sub["billing_period"] == "monthly"
    assert sub["locked_price_usd"] is None
    req = data.tables["plan_change_requests"][0]
    assert req["status"] == "applied" and req["resolved_at"]
    act = data.tables["merchant_activity"]
    assert [a["action"] for a in act] == ["plan_changed"]
    assert act[0]["actor_role"] == "system"
    # idempotent: the same day again does nothing
    assert billing.apply_scheduled_plan_changes(data, TODAY) == 0
    assert len(data.tables["merchant_activity"]) == 1


def test_scheduled_change_in_the_future_waits():
    data = FakeData(subscriptions=[_sub(tier="pro")],
                    plan_change_requests=[_scheduled(effective_at="2026-11-20")])
    assert billing.apply_scheduled_plan_changes(data, TODAY) == 0
    assert data.tables["subscriptions"][0]["tier"] == "pro"
    assert data.tables["plan_change_requests"][0]["status"] == "scheduled"


def test_scheduled_catch_up_applies_past_due_requests():
    data = FakeData(subscriptions=[_sub(tier="pro")],
                    plan_change_requests=[_scheduled(effective_at="2026-11-01")])
    assert billing.apply_scheduled_plan_changes(data, TODAY) == 1


def test_scheduled_annual_target_locks_the_annual_price_at_the_current_phase():
    data = FakeData(
        subscriptions=[_sub(tier="pro", billing_period="monthly")],
        plan_change_requests=[_scheduled(to_period="annual")],
        platform_settings=[{"id": True, "pricing_phase": "list"}])
    billing.apply_scheduled_plan_changes(data, TODAY)
    sub = data.tables["subscriptions"][0]
    assert sub["billing_period"] == "annual"
    assert sub["locked_price_usd"] == float(PLANS["growth"].list_annual_usd)


def test_scheduled_annual_to_monthly_clears_the_lock():
    data = FakeData(
        subscriptions=[_sub(tier="growth", billing_period="annual", locked_price_usd=171.99)],
        plan_change_requests=[_scheduled(from_tier="growth", to_tier="growth",
                                         from_period="annual", to_period="monthly")])
    billing.apply_scheduled_plan_changes(data, TODAY)
    sub = data.tables["subscriptions"][0]
    assert sub["billing_period"] == "monthly" and sub["locked_price_usd"] is None


def test_only_scheduled_requests_are_applied():
    data = FakeData(subscriptions=[_sub(tier="pro")],
                    plan_change_requests=[_scheduled(status="cancelled")])
    assert billing.apply_scheduled_plan_changes(data, TODAY) == 0
    assert data.tables["subscriptions"][0]["tier"] == "pro"


# --------------------------------------------------------------------------- #
# E-mail templates (branding.md: tú, no "!", "datos de acceso", IVA incluido)
# --------------------------------------------------------------------------- #
PAY = {"bank_name": "Banco Pichincha", "account_type": "ahorros",
       "account_number": "2200112233", "beneficiary_name": "conpass SAS"}


def _all_templates(lang, pay=PAY):
    from conpass_common import email_templates as t
    kw = {"owner_name": "Ana", "business_name": "Café <b>Sol</b>"}
    return {
        "activation": t.activation_email(lang, email="a@x.co", temp_password="Tmp-9",
                                         login_url="https://app.conpass.cards/login", **kw),
        "rejected": t.signup_rejected_email(lang, reason="RUC inactivo", **kw),
        "reminder": t.payment_reminder_email(lang, amount_usd=8.52, payment_settings=pay, **kw),
        "change_pay": t.plan_change_payment_email(lang, tier="growth", period="annual",
                                                  amount_usd=171.99, payment_settings=pay, **kw),
        "applied": t.plan_change_applied_email(lang, tier="pro", period="monthly",
                                               next_charge_at="2026-12-19", **kw),
        "change_rejected": t.plan_change_rejected_email(lang, reason="Falta comprobante", **kw),
    }


@pytest.mark.parametrize("lang", ["es", "en"])
def test_templates_follow_the_brand_voice(lang):
    for name, (subject, text, html) in _all_templates(lang).items():
        assert subject and "!" not in subject + text, name
        assert "credencial" not in (subject + text).lower() and "credential" not in text.lower()
        assert "<script" not in html and "Café <b>Sol</b>" not in html     # values are escaped
        assert "Café &lt;b&gt;Sol&lt;/b&gt;" in html or name == "reminder", name


def test_spanish_templates_say_what_they_must():
    t = _all_templates("es")
    assert "Activamos tu cuenta" in t["activation"][1] and "datos de acceso" in t["activation"][1]
    assert "Tmp-9" in t["activation"][1] and "Tmp-9" in t["activation"][2]
    assert "RUC inactivo" in t["rejected"][1]
    assert "$8.52" in t["reminder"][1] and "IVA incluido" in t["reminder"][1]
    assert "2200112233" in t["reminder"][1] and "Banco Pichincha" in t["reminder"][1]
    assert "$171.99" in t["change_pay"][1] and "anual" in t["change_pay"][1]
    assert "Pro" in t["applied"][1] and "2026-12-19" in t["applied"][1]
    assert "Falta comprobante" in t["change_rejected"][1]


def test_english_templates_keep_the_same_intent():
    t = _all_templates("en")
    assert "activated" in t["activation"][0].lower() and "login details" in t["activation"][1]
    assert "$8.52" in t["reminder"][1] and "VAT included" in t["reminder"][1]
    assert "annual" in t["change_pay"][1] and "Reason: RUC inactivo" in t["rejected"][1]


def test_payment_blocks_fall_back_to_a_contact_line_when_unconfigured():
    from conpass_common import email_templates as t
    _, text, _ = t.payment_reminder_email("es", owner_name=None, business_name="Café",
                                          amount_usd=5.99, payment_settings=None)
    assert "info@conpass.cards" in text and "Hola," in text
    # unknown language falls back to Spanish, and the existing credentials mail is untouched
    assert t.signup_rejected_email("fr", owner_name="A", business_name="B", reason="r")[0] == (
        "No pudimos activar tu cuenta de conpass")
    subject, body, _ = t.credentials_email("es", owner_name="A", business_name="B",
                                           email="a@b.co", temp_password="pw", login_url="u")
    assert subject == "Tu cuenta de conpass está lista" and "pw" in body


# --------------------------------------------------------------------------- #
# provisioning: staff role + last sign-in
# --------------------------------------------------------------------------- #
class _AuthAdmin:
    def __init__(self, users):
        self.users, self.updated, self.created = users, [], []

    def get_user_by_id(self, uid):
        if uid == "boom":
            raise RuntimeError("auth down")
        return SimpleNamespace(user=self.users[uid])

    def update_user_by_id(self, uid, attrs):
        self.updated.append((uid, attrs))

    def create_user(self, attrs):
        self.created.append(attrs)
        return SimpleNamespace(user=SimpleNamespace(id="new-user"))


def _auth_client(users):
    data = FakeData()
    data.auth = SimpleNamespace(admin=_AuthAdmin(users))
    return data



def test_last_sign_ins_is_batched_best_effort(monkeypatch):
    from datetime import datetime

    from conpass_common import provisioning
    users = {"a": SimpleNamespace(last_sign_in_at=datetime(2026, 11, 18, 10, 0)),
             "b": SimpleNamespace(last_sign_in_at=None),
             "c": SimpleNamespace(last_sign_in_at="2026-11-01T00:00:00Z")}
    monkeypatch.setattr(provisioning, "service_client", lambda: _auth_client(users))
    out = provisioning.last_sign_ins(["a", "b", "c", "boom", "a"])
    assert out == {"a": "2026-11-18T10:00:00", "c": "2026-11-01T00:00:00Z"}   # b never, boom failed
    assert provisioning.last_sign_ins([]) == {}


def test_last_sign_ins_degrades_to_nothing_without_an_auth_client(monkeypatch):
    from conpass_common import provisioning

    def boom():
        raise RuntimeError("no supabase")
    monkeypatch.setattr(provisioning, "service_client", boom)
    assert provisioning.last_sign_ins(["a"]) == {}


def test_provision_user_writes_the_staff_role_to_the_login_and_the_profile(monkeypatch):
    from conpass_common import provisioning
    client = _auth_client({})
    monkeypatch.setattr(provisioning, "service_client", lambda: client)
    provisioning.provision_user(email="m@x.co", roles=["operation_user"], merchant_id=M1,
                                station="caja_1", name="Mgr", staff_role="manager")
    assert client.auth.admin.created[0]["app_metadata"]["staff_role"] == "manager"
    assert client.tables["profiles"][0]["staff_role"] == "manager"
    # an owner has no staff role: nothing is written
    provisioning.provision_user(email="o@x.co", roles=["merchant_owner"], merchant_id=M1)
    assert "staff_role" not in client.auth.admin.created[1]["app_metadata"]
    assert "staff_role" not in client.tables["profiles"][1]


def test_update_user_merges_the_staff_role_with_the_current_roles(monkeypatch):
    from conpass_common import provisioning
    users = {"u1": SimpleNamespace(app_metadata={"roles": ["operation_user"], "merchant_id": M1,
                                                 "station": "caja_1"})}
    client = _auth_client(users)
    client.tables["profiles"] = [{"user_id": "u1", "staff_role": "cashier"}]
    monkeypatch.setattr(provisioning, "service_client", lambda: client)
    provisioning.update_user("u1", staff_role="manager")
    uid, attrs = client.auth.admin.updated[0]
    assert attrs["app_metadata"] == {"roles": ["operation_user"], "merchant_id": M1,
                                     "station": "caja_1", "staff_role": "manager"}
    assert client.tables["profiles"][0]["staff_role"] == "manager"


def test_update_user_refuses_when_the_current_roles_cannot_be_read(monkeypatch):
    from conpass_common import provisioning
    client = _auth_client({"u1": SimpleNamespace(app_metadata={})})
    monkeypatch.setattr(provisioning, "service_client", lambda: client)
    with pytest.raises(Conflict):
        provisioning.update_user("u1", staff_role="manager")
    assert client.auth.admin.updated == []
