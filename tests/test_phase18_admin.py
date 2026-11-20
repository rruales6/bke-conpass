"""Phase 18 (B3) — admin service: the merchants list/file, verification + activation (D21),
rejection, payments, reminders, plan-change review, platform settings and the stats.
In-memory Data API fake; provisioning, e-mail and wallet are stubbed."""
from __future__ import annotations

from collections import Counter
from datetime import date
from types import SimpleNamespace

import pytest
from _p18_fake import FakeData
from conpass_common import billing, wallet_sync
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.errors import Conflict
from conpass_common.providers.email import EmailDeliveryError
from fastapi.testclient import TestClient

ADMIN = "99999999-9999-9999-9999-999999999999"
M1 = "22222222-2222-2222-2222-222222222222"     # Café Uno: pending signup, receipt, annual Growth
M2 = "66666666-6666-6666-6666-666666666666"     # Gym Dos: paid, monthly Starter
M3 = "33333333-3333-3333-3333-333333333333"     # Tienda Tres: pending, no receipt
OWN1, OWN2 = "o1", "o2"
TODAY = date(2026, 11, 19)


def _as(app, monkeypatch, roles=("platform_admin",)):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=ADMIN, email="admin@conpass.cards", roles=list(roles)))


def _merchant(mid, name, created, **kw):
    return {"id": mid, "business_name": name, "city": "Quito", "ruc": "0999999999001",
            "contact_name": f"Dueño {name}", "contact_email": f"{name[:3].lower()}@x.co",
            "contact_phone": "+593999", "is_demo": False, "created_at": created, **kw}


def _tables():
    return {
        "merchants": [
            _merchant(M1, "Café Uno", "2026-11-10T00:00:00+00:00"),
            _merchant(M2, "Gym Dos", "2026-10-01T00:00:00+00:00"),
            _merchant(M3, "Tienda Tres", "2026-11-15T00:00:00+00:00"),
        ],
        "subscriptions": [
            {"merchant_id": M1, "tier": "growth", "payment_status": "pending",
             "billing_period": "annual"},
            {"merchant_id": M2, "tier": "starter", "payment_status": "paid",
             "billing_period": "monthly", "next_charge_at": "2026-12-01",
             "last_payment_at": "2026-11-01"},
            {"merchant_id": M3, "tier": "starter", "payment_status": "pending",
             "billing_period": "monthly"},
        ],
        "payments": [
            {"id": "pay1", "merchant_id": M1, "kind": "signup", "tier": "growth",
             "billing_period": "annual", "amount_usd": 171.99, "method": "manual_transfer",
             "status": "pending", "proof_storage_key": "payment-proofs/m1.pdf",
             "created_at": "2026-11-10T01:00:00+00:00"},
            {"id": "pay3", "merchant_id": M3, "kind": "signup", "tier": "starter",
             "billing_period": "monthly", "amount_usd": 5.99, "method": "card",
             "status": "pending", "proof_storage_key": None,
             "created_at": "2026-11-15T01:00:00+00:00"},
        ],
        "profiles": [
            {"user_id": OWN1, "merchant_id": M1, "role": "merchant_owner",
             "email": "cafe@uno.co", "name": "Ana"},
            {"user_id": OWN2, "merchant_id": M2, "role": "merchant_owner",
             "email": "gym@dos.co", "name": "Beto"},
        ],
        "merchant_active_passes": [{"merchant_id": M2, "active_pass_count": 250},
                                   {"merchant_id": M1, "active_pass_count": 10}],
        "merchant_locations": [
            {"id": "l1", "merchant_id": M2, "active": True},
            {"id": "l2", "merchant_id": M2, "active": False},
            {"id": "l3", "merchant_id": M1, "active": True}],
        "platform_payment_settings": [{"id": True, "bank_name": "Banco Pichincha",
                                       "account_type": "ahorros", "account_number": "2200112233",
                                       "beneficiary_name": "conpass SAS"}],
    }


class Sender:
    def __init__(self, configured=True, fail=False):
        self.configured, self.fail, self.sent = configured, fail, []

    def send(self, to, subject, text, html=None):
        if self.fail:
            raise EmailDeliveryError("SMTPException")
        self.sent.append((to, subject, text))


@pytest.fixture
def a(monkeypatch):
    import services.admin.handler as h

    data = FakeData(**_tables())
    sender = Sender()
    issued = []
    box = SimpleNamespace(h=h, data=data, sender=sender, issued=issued, synced=[],
                          mp=monkeypatch)
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(data))
    monkeypatch.setattr(h, "get_email_sender", lambda: box.sender)
    monkeypatch.setattr(h, "get_user_language", lambda uid: "es")
    monkeypatch.setattr(h, "issue_temp_password", lambda uid: issued.append(uid) or "Temp-pw-9")
    monkeypatch.setattr(billing, "today_utc", lambda: TODAY)
    monkeypatch.setattr(wallet_sync, "sync_merchant_classes",
                        lambda c, mid: box.synced.append(mid))
    _as(h.app, monkeypatch)
    box.c = TestClient(h.app)
    return box


def sub_of(a, mid):
    return next(s for s in a.data.tables["subscriptions"] if s["merchant_id"] == mid)


def actions(a, mid=None):
    return [r["action"] for r in a.data.tables.get("merchant_activity", [])
            if mid is None or r["merchant_id"] == mid]


# --------------------------------------------------------------------------- #
# GET /admin/clients
# --------------------------------------------------------------------------- #
def test_clients_carry_the_new_fields(a):
    body = {c["merchantId"]: c for c in a.c.get("/admin/clients").json()}
    c1, c2 = body[M1], body[M2]
    assert (c1["ruc"], c1["contactName"], c1["contactEmail"], c1["contactPhone"]) == (
        "0999999999001", "Dueño Café Uno", "caf@x.co", "+593999")
    assert c1["billingPeriod"] == "annual" and c2["billingPeriod"] == "monthly"
    assert c2["locationCount"] == 1 and c1["locationCount"] == 1      # only active branches
    assert c2["notificationsUsed"] == 0
    # priceUsd is the period price; the estimate follows the shared v3.0 rules
    assert c2["priceUsd"] == 5.99 and c2["estimatedMonthlyUsd"] == 8.52   # 250 cards: +50 extra
    assert c1["priceUsd"] == 171.99 and c1["estimatedMonthlyUsd"] == 171.99
    assert c2["nextChargeAt"] == "2026-12-01" and c2["lastPaymentAt"] == "2026-11-01"


def test_pending_verification_needs_pending_status_and_a_receipt(a):
    body = {c["merchantId"]: c for c in a.c.get("/admin/clients").json()}
    assert body[M1]["pendingVerification"] is True and body[M1]["hasPaymentProof"] is True
    assert body[M3]["pendingVerification"] is False and body[M3]["hasPaymentProof"] is False
    assert body[M2]["pendingVerification"] is False
    # a paid merchant with a receipt is not "por verificar"
    a.data.tables["payments"].append({
        "id": "pay2", "merchant_id": M2, "kind": "signup", "amount_usd": 5.99,
        "method": "deuna", "status": "confirmed", "proof_storage_key": "k",
        "created_at": "2026-10-01T00:00:00+00:00"})
    body = {c["merchantId"]: c for c in a.c.get("/admin/clients").json()}
    assert body[M2]["pendingVerification"] is False and body[M2]["hasPaymentProof"] is True
    # the legacy column still counts as a receipt
    sub_of(a, M3)["payment_proof_key"] = "legacy"
    body = {c["merchantId"]: c for c in a.c.get("/admin/clients").json()}
    assert body[M3]["pendingVerification"] is True


def test_has_pending_plan_change_only_for_requests_in_review(a):
    a.data.tables["plan_change_requests"] = [
        {"id": "r1", "merchant_id": M2, "status": "pending_review"},
        {"id": "r2", "merchant_id": M1, "status": "awaiting_payment"}]
    body = {c["merchantId"]: c for c in a.c.get("/admin/clients").json()}
    assert body[M2]["hasPendingPlanChange"] is True and body[M1]["hasPendingPlanChange"] is False


def test_clients_listing_is_batched(a):
    calls = Counter()
    real = a.data.table
    a.data.table = lambda name: (calls.update([name]), real(name))[1]
    for i in range(20):                                   # many merchants, still one query each
        a.data.tables["merchants"].append(_merchant(f"mm{i}", f"Extra {i}", "2026-11-01T00:00:00Z"))
        a.data.tables["subscriptions"].append(
            {"merchant_id": f"mm{i}", "tier": "starter", "payment_status": "paid"})
    assert len(a.c.get("/admin/clients").json()) == 23
    assert max(calls.values()) <= 2, calls


def test_clients_require_platform_admin(a):
    _as(a.h.app, a.mp, roles=("merchant_owner",))
    assert a.c.get("/admin/clients").status_code == 403


# --------------------------------------------------------------------------- #
# GET /admin/stats
# --------------------------------------------------------------------------- #
def test_stats_aggregates(a):
    a.data.tables["subscriptions"] += [
        {"merchant_id": "x1", "tier": "pro", "payment_status": "paid",
         "billing_period": "annual", "locked_price_usd": 555.99, "next_charge_at": "2027-05-01"},
        {"merchant_id": "x2", "tier": "growth", "payment_status": "overdue"},
        {"merchant_id": "x3", "tier": "growth", "payment_status": "suspended"},
        {"merchant_id": "x4", "tier": "starter", "payment_status": "rejected"}]
    a.data.tables["plan_change_requests"] = [
        {"id": "r1", "merchant_id": M2, "status": "pending_review"},
        {"id": "r2", "merchant_id": "x1", "status": "pending_review"},
        {"id": "r3", "merchant_id": "x2", "status": "scheduled"}]
    b = a.c.get("/admin/stats").json()
    assert b["activeClients"] == 2                                   # M2 and x1
    # M2 monthly 5.99 + x1 annual 555.99 / 12 = 46.33
    assert b["subscriptionMrrUsd"] == pytest.approx(5.99 + 46.33, abs=0.01)
    # mrr = paid estimates: M2 8.52 (this month) + x1 0 (annual, not its renewal month)
    assert b["mrrUsd"] == pytest.approx(8.52)
    assert b["tierCounts"] == {"starter": 3, "growth": 3, "pro": 1}
    assert b["overdueCount"] == 1 and b["suspendedCount"] == 1
    assert b["pendingVerificationCount"] == 1                        # M1 (M3 has no receipt)
    assert b["pendingPlanChangeCount"] == 2


def test_stats_follow_the_pricing_phase(a):
    a.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    b = a.c.get("/admin/stats").json()
    assert b["subscriptionMrrUsd"] == 7.99


# --------------------------------------------------------------------------- #
# GET /admin/clients/{id}
# --------------------------------------------------------------------------- #
def test_client_detail(a):
    a.data.tables["plan_change_requests"] = [{
        "id": "r1", "merchant_id": M2, "from_tier": "starter", "to_tier": "growth",
        "from_period": "monthly", "to_period": "monthly", "direction": "upgrade",
        "status": "pending_review", "amount_usd": 17.99, "payment_id": "pc",
        "created_at": "2026-11-18T00:00:00Z"}]
    a.data.tables["payments"].append({
        "id": "pc", "merchant_id": M2, "kind": "plan_change", "tier": "growth",
        "billing_period": "monthly", "amount_usd": 17.99, "method": "deuna",
        "status": "pending", "proof_storage_key": "k", "created_at": "2026-11-18T01:00:00Z"})
    for i in range(60):
        a.data.tables.setdefault("merchant_activity", []).append({
            "id": f"a{i:02d}", "merchant_id": M2, "action": "payment_recorded",
            "actor_role": "platform_admin", "details": {}, "created_at": f"2026-11-{i % 28 + 1:02d}"
            f"T00:00:{i:02d}Z"})
    b = a.c.get(f"/admin/clients/{M2}").json()
    assert b["client"]["merchantId"] == M2 and b["client"]["hasPendingPlanChange"] is True
    assert b["merchant"]["businessName"] == "Gym Dos" and b["merchant"]["contactPhone"] == "+593999"
    assert b["subscription"]["tier"] == "starter" and b["subscription"]["activeCards"] == 250
    assert b["subscription"]["pendingChange"]["id"] == "r1"
    assert b["planChange"]["status"] == "pending_review" and b["planChange"]["hasReceipt"] is True
    assert b["usage"]["activeCards"] == 250 and b["usage"]["locationCount"] == 1
    assert b["ownerEmail"] == "gym@dos.co"
    assert [p["id"] for p in b["payments"]] == ["pc"]
    assert len(b["activity"]) == 50
    stamps = [x["createdAt"] for x in b["activity"]]
    assert stamps == sorted(stamps, reverse=True)


def test_client_detail_unknown_is_404(a):
    assert a.c.get("/admin/clients/77777777-7777-7777-7777-777777777777").status_code == 404


# --------------------------------------------------------------------------- #
# POST .../activate
# --------------------------------------------------------------------------- #
def _activate(a, mid=M1, ruc=True, proof=True):
    return a.c.post(f"/admin/clients/{mid}/activate",
                    json={"rucVerified": ruc, "proofVerified": proof})


@pytest.mark.parametrize("ruc,proof", [(False, True), (True, False), (False, False)])
def test_activation_needs_both_verifications(a, ruc, proof):
    r = _activate(a, ruc=ruc, proof=proof)
    assert r.status_code == 422 and r.json()["details"]["reason"] == "verification_incomplete"
    assert sub_of(a, M1)["payment_status"] == "pending" and a.sender.sent == [] and a.issued == []


def test_activation_commits_then_emails_the_access_details(a):
    r = _activate(a)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["subscription"]["paymentStatus"] == "paid"
    sub = sub_of(a, M1)
    assert sub["payment_status"] == "paid" and sub["last_payment_at"] == "2026-11-19"
    assert sub["next_charge_at"] == "2027-11-19"                      # annual: + 1 year
    assert sub["locked_price_usd"] == 171.99                          # annual price at launch
    pay = next(p for p in a.data.tables["payments"] if p["id"] == "pay1")
    assert pay["status"] == "confirmed" and pay["paid_at"] == "2026-11-19"
    assert pay["confirmed_by"] == ADMIN and pay["confirmed_at"]
    assert actions(a, M1) == ["activated"]
    assert a.issued == [OWN1]
    to, subject, text = a.sender.sent[0]
    assert to == "cafe@uno.co" and "Activamos tu cuenta" in text
    assert "Temp-pw-9" in text and "datos de acceso" in text and "!" not in text
    assert "credencial" not in text.lower()
    assert b["payments"][0]["status"] == "confirmed"


def test_activation_of_a_monthly_account(a):
    sub_of(a, M3)["tier"] = "starter"
    a.data.tables["profiles"].append({"user_id": "o3", "merchant_id": M3, "role": "merchant_owner",
                                      "email": "t@3.co", "name": "T"})
    assert _activate(a, M3).status_code == 200
    sub = sub_of(a, M3)
    assert sub["next_charge_at"] == "2026-12-19" and sub.get("locked_price_usd") is None
    assert next(p for p in a.data.tables["payments"] if p["id"] == "pay3")["status"] == "confirmed"


def test_activation_annual_lock_uses_the_list_price_in_the_list_phase(a):
    a.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    _activate(a)
    assert sub_of(a, M1)["locked_price_usd"] == 219.99


def test_activation_clears_a_previous_rejection(a):
    sub_of(a, M1).update(payment_status="rejected", rejection_reason="RUC inactivo")
    assert _activate(a).status_code == 200
    assert sub_of(a, M1)["rejection_reason"] is None


def test_activation_conflicts_when_already_paid(a):
    r = _activate(a, M2)
    assert r.status_code == 409 and a.issued == []


def test_activation_email_failure_is_502_but_the_activation_stays(a):
    a.sender.fail = True
    r = _activate(a)
    assert r.status_code == 502 and r.json()["code"] == "email_delivery_failed"
    sub = sub_of(a, M1)
    assert sub["payment_status"] == "paid" and sub["next_charge_at"] == "2027-11-19"
    assert next(p for p in a.data.tables["payments"] if p["id"] == "pay1")["status"] == "confirmed"
    assert actions(a, M1) == ["activated"]


def test_activation_with_email_not_configured_keeps_the_state_and_the_old_password(a):
    a.sender.configured = False
    r = _activate(a)
    assert r.status_code == 502 and r.json()["code"] == "email_delivery_failed"
    assert sub_of(a, M1)["payment_status"] == "paid"
    assert a.issued == []                          # the password was not rotated


def test_activation_password_failure_is_also_502(a):
    a.mp.setattr(a.h, "issue_temp_password", lambda uid: (_ for _ in ()).throw(Conflict("x")))
    r = _activate(a)
    assert r.status_code == 502 and sub_of(a, M1)["payment_status"] == "paid"


def test_activation_without_a_ledger_row_records_one(a):
    a.data.tables["payments"] = [p for p in a.data.tables["payments"] if p["merchant_id"] != M1]
    assert _activate(a).status_code == 200
    pay = next(p for p in a.data.tables["payments"] if p["merchant_id"] == M1)
    assert pay["kind"] == "signup" and pay["status"] == "confirmed" and pay["amount_usd"] == 171.99


def test_activation_refuses_the_demo_and_a_merchant_without_owner(a):
    a.data.tables["merchants"][0]["is_demo"] = True
    assert _activate(a).status_code == 409
    a.data.tables["merchants"][0]["is_demo"] = False
    a.data.tables["profiles"] = [p for p in a.data.tables["profiles"] if p["merchant_id"] != M1]
    assert _activate(a).status_code == 404
    assert sub_of(a, M1)["payment_status"] == "pending"      # nothing committed


# --------------------------------------------------------------------------- #
# POST .../reject
# --------------------------------------------------------------------------- #
def test_reject_saves_the_state_and_emails_the_reason(a):
    r = a.c.post(f"/admin/clients/{M1}/reject", json={"reason": "El RUC no está activo"})
    assert r.status_code == 200, r.text
    sub = sub_of(a, M1)
    assert sub["payment_status"] == "rejected"
    assert sub["rejection_reason"] == "El RUC no está activo"
    pay = next(p for p in a.data.tables["payments"] if p["id"] == "pay1")
    assert pay["status"] == "rejected" and pay["rejection_reason"] == "El RUC no está activo"
    to, subject, text = a.sender.sent[0]
    assert to == "cafe@uno.co" and "El RUC no está activo" in text and "!" not in text
    assert actions(a, M1) == ["rejected"]
    assert r.json()["subscription"]["rejectionReason"] == "El RUC no está activo"


def test_reject_email_failure_is_502_with_the_state_saved(a):
    a.sender.fail = True
    r = a.c.post(f"/admin/clients/{M1}/reject", json={"reason": "Comprobante ilegible"})
    assert r.status_code == 502 and r.json()["code"] == "email_delivery_failed"
    assert sub_of(a, M1)["payment_status"] == "rejected"
    assert actions(a, M1) == ["rejected"]


def test_reject_validates_and_refuses_an_active_account(a):
    assert a.c.post(f"/admin/clients/{M1}/reject", json={"reason": ""}).status_code == 422
    assert a.c.post(f"/admin/clients/{M1}/reject", json={"reason": "   "}).status_code == 422
    assert a.c.post(f"/admin/clients/{M2}/reject", json={"reason": "x"}).status_code == 409


# --------------------------------------------------------------------------- #
# POST .../payments
# --------------------------------------------------------------------------- #
def _record(a, mid=M2, **over):
    body = {"kind": "renewal", "amountUsd": 5.99, "method": "manual_transfer",
            "paidAt": "2026-11-30"}
    body.update(over)
    return a.c.post(f"/admin/clients/{mid}/payments", json=body)


def test_renewal_payment_moves_the_next_charge_one_period(a):
    r = _record(a)
    assert r.status_code == 201, r.text
    b = r.json()
    assert (b["kind"], b["status"], b["amountUsd"], b["paidAt"]) == (
        "renewal", "confirmed", 5.99, "2026-11-30")
    sub = sub_of(a, M2)
    assert sub["payment_status"] == "paid" and sub["last_payment_at"] == "2026-11-30"
    assert sub["next_charge_at"] == "2027-01-01"          # from the CURRENT next charge, not paidAt
    pay = a.data.tables["payments"][-1]
    assert pay["confirmed_by"] == ADMIN and pay["period_end"] == "2027-01-01"
    assert actions(a, M2) == ["payment_recorded"]


def test_renewal_without_a_next_charge_starts_from_the_payment_date(a):
    sub_of(a, M2)["next_charge_at"] = None
    _record(a, paidAt="2026-11-30")
    assert sub_of(a, M2)["next_charge_at"] == "2026-12-30"


def test_annual_renewal_refreshes_the_locked_price_at_the_current_phase(a):
    sub_of(a, M2).update(billing_period="annual", locked_price_usd=56.99,
                         next_charge_at="2026-11-25")
    a.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    _record(a, amountUsd=75.99, paidAt="2026-11-24")
    sub = sub_of(a, M2)
    assert sub["next_charge_at"] == "2027-11-25" and sub["locked_price_usd"] == 75.99


def test_non_renewal_payment_does_not_move_the_charge_date(a):
    r = _record(a, kind="overage", amountUsd=2.53, note="50 tarjetas extra")
    assert r.status_code == 201 and r.json()["note"] == "50 tarjetas extra"
    sub = sub_of(a, M2)
    assert sub["next_charge_at"] == "2026-12-01" and sub["last_payment_at"] == "2026-11-30"


def test_a_signup_kind_is_not_accepted_here(a):
    assert _record(a, kind="signup").status_code == 422
    assert _record(a, amountUsd=-1).status_code == 422


# --------------------------------------------------------------------------- #
# POST .../payment-reminder
# --------------------------------------------------------------------------- #
def test_reminder_emails_the_amount_due_with_the_payment_details(a):
    r = a.c.post(f"/admin/clients/{M2}/payment-reminder")
    assert r.status_code == 200 and r.json() == {"sent": True, "email": "gym@dos.co"}
    to, subject, text = a.sender.sent[0]
    assert to == "gym@dos.co" and "$8.52" in text and "IVA incluido" in text
    assert "2200112233" in text and "Banco Pichincha" in text and "!" not in text
    assert actions(a, M2) == ["reminder_sent"]
    assert a.data.tables["merchant_activity"][0]["details"] == {"amountUsd": 8.52}


def test_reminder_gates(a):
    a.data.tables["merchants"][1]["is_demo"] = True
    assert a.c.post(f"/admin/clients/{M2}/payment-reminder").status_code == 409
    a.data.tables["merchants"][1]["is_demo"] = False
    a.sender.configured = False
    assert a.c.post(f"/admin/clients/{M2}/payment-reminder").status_code == 503
    a.sender.configured, a.sender.fail = True, True
    r = a.c.post(f"/admin/clients/{M2}/payment-reminder")
    assert r.status_code == 502 and r.json()["code"] == "email_delivery_failed"
    assert actions(a, M2) == []                  # nothing logged for a reminder that was not sent


# --------------------------------------------------------------------------- #
# Plan-change review
# --------------------------------------------------------------------------- #
def _request(a, status="pending_review", direction="upgrade", to_tier="growth",
             to_period="monthly", payment=True):
    a.data.tables["payments"].append({
        "id": "pc", "merchant_id": M2, "kind": "plan_change", "tier": to_tier,
        "billing_period": to_period, "amount_usd": 17.99, "method": "manual_transfer",
        "status": "pending", "proof_storage_key": "k", "created_at": "2026-11-18T01:00:00Z"})
    a.data.tables["plan_change_requests"] = [{
        "id": "r1", "merchant_id": M2, "from_tier": "starter", "to_tier": to_tier,
        "from_period": "monthly", "to_period": to_period, "direction": direction,
        "status": status, "amount_usd": 17.99, "payment_id": "pc" if payment else None,
        "created_at": "2026-11-18T00:00:00Z"}]


def test_approve_applies_the_new_plan_from_today(a):
    _request(a)
    r = a.c.post("/admin/plan-changes/r1/approve")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied" and r.json()["hasReceipt"] is True
    sub = sub_of(a, M2)
    assert sub["tier"] == "growth" and sub["billing_period"] == "monthly"
    assert sub["next_charge_at"] == "2026-12-19" and sub["last_payment_at"] == "2026-11-19"
    assert sub["locked_price_usd"] is None
    pay = next(p for p in a.data.tables["payments"] if p["id"] == "pc")
    assert pay["status"] == "confirmed" and pay["paid_at"] == "2026-11-19"
    assert pay["confirmed_by"] == ADMIN
    req = a.data.tables["plan_change_requests"][0]
    assert req["status"] == "applied" and req["resolved_by"] == ADMIN and req["resolved_at"]
    assert actions(a, M2) == ["plan_changed"]
    assert a.synced == [M2]                                   # wallet classes follow the tier
    to, subject, text = a.sender.sent[0]
    assert to == "gym@dos.co" and "Growth" in text and "2026-12-19" in text


def test_approve_an_annual_upgrade_locks_the_price(a):
    _request(a, to_tier="pro", to_period="annual", status="awaiting_payment")
    assert a.c.post("/admin/plan-changes/r1/approve").status_code == 200
    sub = sub_of(a, M2)
    assert sub["billing_period"] == "annual" and sub["locked_price_usd"] == 555.99
    assert sub["next_charge_at"] == "2027-11-19"


def test_approve_only_from_review_or_awaiting_payment(a):
    for status in ("scheduled", "applied", "rejected", "cancelled"):
        _request(a, status=status)
        r = a.c.post("/admin/plan-changes/r1/approve")
        assert r.status_code == 409, status
        assert sub_of(a, M2)["tier"] == "starter"
    assert a.c.post("/admin/plan-changes/nope/approve").status_code == 404


def test_approving_twice_is_a_conflict(a):
    _request(a)
    assert a.c.post("/admin/plan-changes/r1/approve").status_code == 200
    assert a.c.post("/admin/plan-changes/r1/approve").status_code == 409
    assert actions(a, M2) == ["plan_changed"]


def test_approve_survives_an_email_failure(a):
    _request(a)
    a.sender.fail = True
    assert a.c.post("/admin/plan-changes/r1/approve").status_code == 200
    assert sub_of(a, M2)["tier"] == "growth"


def test_reject_a_plan_change(a):
    _request(a)
    r = a.c.post("/admin/plan-changes/r1/reject", json={"reason": "Falta el comprobante"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert r.json()["rejectionReason"] == "Falta el comprobante"
    assert sub_of(a, M2)["tier"] == "starter"
    pay = next(p for p in a.data.tables["payments"] if p["id"] == "pc")
    assert pay["status"] == "rejected" and pay["rejection_reason"] == "Falta el comprobante"
    assert actions(a, M2) == ["plan_change_rejected"]
    assert "Falta el comprobante" in a.sender.sent[0][2]
    assert a.c.post("/admin/plan-changes/r1/reject", json={"reason": "x"}).status_code == 409
    assert a.synced == []


def test_reject_a_plan_change_needs_a_reason(a):
    _request(a)
    assert a.c.post("/admin/plan-changes/r1/reject", json={}).status_code == 422
    assert a.c.post("/admin/plan-changes/r1/reject", json={"reason": " "}).status_code == 422


# --------------------------------------------------------------------------- #
# Platform settings, subscription activity, receipts
# --------------------------------------------------------------------------- #
def test_platform_settings_get_and_patch(a):
    a.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "launch",
                                           "list_price_effective_at": None}]
    b = a.c.get("/admin/platform-settings").json()
    assert b["pricingPhase"] == "launch" and "listPriceEffectiveAt" not in b
    assert b["paymentSettings"]["configured"] is True
    assert b["paymentSettings"]["bankName"] == "Banco Pichincha"
    b = a.c.patch("/admin/platform-settings", json={
        "pricingPhase": "list", "listPriceEffectiveAt": "2026-12-20"}).json()
    assert b["pricingPhase"] == "list" and b["listPriceEffectiveAt"] == "2026-12-20"
    # partial: the date stays when only the phase is sent; an explicit null clears it
    assert a.c.patch("/admin/platform-settings", json={"pricingPhase": "launch"}
                     ).json()["listPriceEffectiveAt"] == "2026-12-20"
    cleared = a.c.patch("/admin/platform-settings", json={"listPriceEffectiveAt": None}).json()
    assert "listPriceEffectiveAt" not in cleared
    assert a.c.patch("/admin/platform-settings", json={"pricingPhase": "free"}).status_code == 422


def test_platform_settings_default_when_the_row_is_missing(a):
    b = a.c.get("/admin/platform-settings").json()
    assert b["pricingPhase"] == "launch"
    # the first PATCH creates the singleton
    assert a.c.patch("/admin/platform-settings", json={"pricingPhase": "list"}
                     ).json()["pricingPhase"] == "list"
    assert a.data.tables["platform_settings"][0]["id"] is True


def test_subscription_patch_logs_suspension_and_reactivation(a):
    patch = lambda body: a.c.patch(f"/admin/clients/{M2}/subscription", json=body)  # noqa: E731
    assert patch({"paymentStatus": "suspended"}).status_code == 200
    assert patch({"paymentStatus": "suspended"}).status_code == 200      # unchanged: no new row
    assert patch({"paymentStatus": "paid"}).status_code == 200
    assert patch({"paymentStatus": "overdue"}).status_code == 200        # neither to nor from
    assert patch({"tier": "growth"}).status_code == 200
    assert actions(a, M2) == ["suspended", "reactivated"]
    assert a.synced == [M2]


def test_resend_credentials_is_logged(a):
    a.mp.setattr(a.h, "get_user_language", lambda uid: "en")
    assert a.c.post(f"/admin/clients/{M2}/resend-credentials").status_code == 200
    assert actions(a, M2) == ["credentials_resent"]


def test_payment_proof_prefers_the_ledger_and_falls_back_to_the_legacy_column(a):
    a.mp.setattr(a.h, "presign_payment_proof_download", lambda key: f"https://s3/{key}")
    sub_of(a, M1)["payment_proof_key"] = "legacy.pdf"
    b = a.c.get(f"/admin/clients/{M1}/payment-proof").json()
    assert b["url"] == "https://s3/payment-proofs/m1.pdf"
    assert b["uploadedAt"] == "2026-11-10T01:00:00+00:00"
    a.data.tables["payments"] = [p for p in a.data.tables["payments"] if p["merchant_id"] != M1]
    sub_of(a, M1)["payment_proof_uploaded_at"] = "2026-11-10T00:00:00+00:00"
    assert a.c.get(f"/admin/clients/{M1}/payment-proof").json()["url"] == "https://s3/legacy.pdf"
    assert a.c.get(f"/admin/clients/{M3}/payment-proof").status_code == 404


def test_payment_receipt_endpoint_is_scoped(a):
    a.mp.setattr("conpass_common.assets.presign_payment_proof_download",
                 lambda key: f"https://s3/{key}")
    assert a.c.get(f"/admin/clients/{M1}/payments/pay1/receipt").json()["url"] == (
        "https://s3/payment-proofs/m1.pdf")
    assert a.c.get(f"/admin/clients/{M2}/payments/pay1/receipt").status_code == 404
    assert a.c.get(f"/admin/clients/{M3}/payments/pay3/receipt").status_code == 404   # no key


@pytest.mark.parametrize("method,path,body", [
    ("get", "/admin/clients/{m}", None),
    ("post", "/admin/clients/{m}/activate", {"rucVerified": True, "proofVerified": True}),
    ("post", "/admin/clients/{m}/reject", {"reason": "x"}),
    ("post", "/admin/clients/{m}/payments",
     {"kind": "renewal", "amountUsd": 1, "method": "cash", "paidAt": "2026-11-01"}),
    ("get", "/admin/clients/{m}/payments/pay1/receipt", None),
    ("post", "/admin/clients/{m}/payment-reminder", None),
    ("post", "/admin/plan-changes/r1/approve", None),
    ("post", "/admin/plan-changes/r1/reject", {"reason": "x"}),
    ("get", "/admin/platform-settings", None),
    ("patch", "/admin/platform-settings", {"pricingPhase": "list"}),
    ("get", "/admin/stats", None),
])
def test_every_admin_endpoint_requires_platform_admin(a, method, path, body):
    for roles in (("merchant_owner",), ("operation_user",)):
        _as(a.h.app, a.mp, roles=roles)
        r = getattr(a.c, method)(path.format(m=M1), **({"json": body} if body else {}))
        assert r.status_code == 403, (path, roles)
    assert a.sender.sent == [] and a.issued == []
