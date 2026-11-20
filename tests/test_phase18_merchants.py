"""Phase 18 (B3) — merchants service: catalog/usage, onboarding (D21), branch limits, team with
manager scoping, payments and plan changes (O4). In-memory Data API fake + monkeypatched
provisioning/e-mail/wallet: nothing here touches Supabase, Google, S3 or SMTP."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from _p18_fake import FakeData
from conpass_common import billing, wallet_sync
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.errors import Conflict
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"       # the owner
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
MGR = "77777777-7777-7777-7777-777777777777"     # a manager
CA = "44444444-4444-4444-4444-444444444444"       # cashier in branch A
CB = "55555555-5555-5555-5555-555555555555"       # cashier in branch B
CAB = "88888888-8888-8888-8888-888888888888"      # cashier in both A and B
LA = "aaaaaaaa-0000-0000-0000-00000000000a"
LB = "aaaaaaaa-0000-0000-0000-00000000000b"
LC = "aaaaaaaa-0000-0000-0000-00000000000c"
TODAY = date(2026, 11, 19)


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1, user_id=U1,
              email="owner@x.co"):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=user_id, email=email, roles=list(roles), merchant_id=merchant_id))


def _loc(lid, name, active=True, merchant=M1, n=0):
    return {"id": lid, "merchant_id": merchant, "name": name, "latitude": -0.18,
            "longitude": -78.48, "active": active, "created_at": f"2026-11-0{n + 1}T00:00:00+00:00"}


def _profile(uid, role="operation_user", staff_role="cashier", name="Op", merchant=M1):
    return {"user_id": uid, "merchant_id": merchant, "role": role, "staff_role": staff_role,
            "name": name, "email": f"{name.lower()}@x.co", "station": "caja_1"}


def _tables(tier="starter", **sub):
    return {
        "merchants": [{"id": M1, "business_name": "Café Uno", "ruc": "0999999999001",
                       "category": "cafe_restaurant", "city": "Quito", "contact_name": "Ana",
                       "contact_email": "ana@x.co", "contact_phone": "+593999",
                       "is_demo": False, "created_at": "2026-10-01T00:00:00+00:00"}],
        "subscriptions": [{"merchant_id": M1, "tier": tier, "payment_status": "paid",
                           "billing_period": "monthly", "next_charge_at": "2026-12-05",
                           "last_payment_at": "2026-11-05", **sub}],
        "merchant_locations": [_loc(LA, "Centro", n=0)],
        "profiles": [_profile(U1, "merchant_owner", None, "Ana")],
        "operation_user_locations": [],
        "platform_payment_settings": [{"id": True, "bank_name": "Banco Pichincha",
                                       "account_type": "ahorros", "account_number": "2200112233",
                                       "beneficiary_name": "conpass SAS"}],
    }


class Sender:
    configured = True

    def __init__(self):
        self.sent: list[tuple] = []

    def send(self, to, subject, text, html=None):
        self.sent.append((to, subject, text))


@pytest.fixture
def m(monkeypatch):
    """The merchants handler over an in-memory client, with every side effect stubbed."""
    import services.merchants.handler as h
    from services.merchants.repository import MerchantsRepository

    data = FakeData(**_tables())
    repo = MerchantsRepository(data)
    sender, provisioned, synced = Sender(), [], []
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_email_sender", lambda: sender)
    monkeypatch.setattr(h, "get_user_language", lambda uid: "es")
    monkeypatch.setattr(h, "last_sign_ins", lambda ids: {})
    monkeypatch.setattr(billing, "today_utc", lambda: TODAY)
    monkeypatch.setattr(wallet_sync, "sync_merchant_classes", lambda c, mid: synced.append(mid))

    def fake_provision(**kw):
        provisioned.append(kw)
        return SimpleNamespace(user_id=f"new-{len(provisioned)}", email=kw["email"],
                               temp_password="tmp-pass-1")
    monkeypatch.setattr(h, "provision_user", fake_provision)
    monkeypatch.setattr(h, "get_payment_provider",
                        lambda: SimpleNamespace(submit=lambda intent: None))
    _identity(h.app, monkeypatch)
    return SimpleNamespace(h=h, data=data, sender=sender, provisioned=provisioned,
                           synced=synced, c=TestClient(h.app), mp=monkeypatch)


def tier(m, name):
    m.data.tables["subscriptions"][0]["tier"] = name


# --------------------------------------------------------------------------- #
# A. Catalog and usage
# --------------------------------------------------------------------------- #
def test_plans_follow_the_pricing_phase(m):
    body = m.c.get("/plans").json()
    assert [p["tier"] for p in body] == ["starter", "growth", "pro"]
    assert body[0]["priceUsd"] == 5.99 and body[0]["listPriceUsd"] == 7.99
    assert body[0]["pricingPhase"] == "launch" and body[2]["annualPriceUsd"] == 555.99
    m.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    body = m.c.get("/plans").json()
    assert body[0]["priceUsd"] == 7.99 and body[0]["pricingPhase"] == "list"


def test_usage_monthly(m):
    m.data.tables["merchant_active_passes"] = [{"merchant_id": M1, "active_pass_count": 250}]
    b = m.c.get(f"/merchants/{M1}/usage").json()
    assert b["subscriptionUsd"] == 5.99 and b["billingPeriod"] == "monthly"
    assert b["estimatedTotalUsd"] == 8.52 and b["ivaUsd"] == pytest.approx(1.11)
    assert b["locationCount"] == 1


def test_usage_annual_bills_the_subscription_only_in_the_renewal_month(m):
    sub = m.data.tables["subscriptions"][0]
    sub.update(billing_period="annual", locked_price_usd=56.99, next_charge_at="2027-03-01")
    assert m.c.get(f"/merchants/{M1}/usage").json()["subscriptionUsd"] == 0
    sub["next_charge_at"] = "2026-11-30"
    b = m.c.get(f"/merchants/{M1}/usage").json()
    assert b["subscriptionUsd"] == 56.99 and b["estimatedTotalUsd"] == 56.99


def test_usage_is_tenant_scoped(m):
    _identity(m.h.app, m.mp, merchant_id=M2)
    assert m.c.get(f"/merchants/{M1}/usage").status_code == 403
    _identity(m.h.app, m.mp, roles=("platform_admin",), merchant_id=None)
    assert m.c.get(f"/merchants/{M1}/usage").status_code == 200


def test_get_merchant_includes_phone_and_pending_change(m):
    m.data.tables["plan_change_requests"] = [{
        "id": "r1", "merchant_id": M1, "from_tier": "starter", "to_tier": "growth",
        "from_period": "monthly", "to_period": "monthly", "direction": "upgrade",
        "status": "awaiting_payment", "amount_usd": 17.99, "created_at": "2026-11-18T00:00:00Z"}]
    b = m.c.get(f"/merchants/{M1}").json()
    assert b["contactPhone"] == "+593999"
    assert b["subscription"]["billingPeriod"] == "monthly"
    assert b["subscription"]["pendingChange"]["status"] == "awaiting_payment"
    assert b["subscription"]["pendingChange"]["amountUsd"] == 17.99


# --------------------------------------------------------------------------- #
# B. Onboarding (D21: nothing is e-mailed at signup)
# --------------------------------------------------------------------------- #
def _onboard(**over):
    body = {"businessName": "Barbería Sur", "ruc": "0999999999001", "category": "beauty",
            "city": "Quito", "contactName": "Luis", "contactEmail": "luis@test.co",
            "contactPhone": "+593988", "tier": "growth",
            "payment": {"method": "manual_transfer", "proofStorageKey": "payment-proofs/a.pdf"}}
    body.update(over)
    return body


def test_onboard_records_the_signup_and_sends_no_email(m):
    m.data.tables["merchants"].clear()
    m.data.tables["subscriptions"].clear()
    m.data.tables["profiles"].clear()
    r = m.c.post("/merchants", json=_onboard())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ownerInviteSent"] is False and body["ownerEmail"] == "luis@test.co"
    assert m.sender.sent == []              # access details wait for activation
    assert body["merchant"]["contactPhone"] == "+593988"
    assert body["subscription"]["billingPeriod"] == "monthly"    # the default
    merchant = m.data.tables["merchants"][0]
    assert merchant["contact_phone"] == "+593988" and merchant["category"] == "beauty"
    sub = m.data.tables["subscriptions"][0]
    assert sub["billing_period"] == "monthly"
    assert sub["payment_proof_key"] == "payment-proofs/a.pdf"    # legacy column kept one release
    pay = m.data.tables["payments"][0]
    assert (pay["kind"], pay["status"], pay["method"]) == ("signup", "pending", "manual_transfer")
    assert pay["amount_usd"] == 17.99 and pay["tier"] == "growth"
    assert pay["proof_storage_key"] == "payment-proofs/a.pdf"
    assert [a["action"] for a in m.data.tables["merchant_activity"]] == [
        "signed_up", "proof_uploaded"]
    # the owner login is still created (with a forced password change), and must_change is on
    assert m.provisioned[0]["roles"] == ["merchant_owner"]
    assert m.provisioned[0]["must_change_password"] is True


def test_onboard_annual_amount_follows_the_phase(m):
    r = m.c.post("/merchants", json=_onboard(billingPeriod="annual"))
    assert r.status_code == 201
    assert m.data.tables["payments"][-1]["amount_usd"] == 171.99
    assert r.json()["subscription"]["priceUsd"] == 171.99
    m.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    m.c.post("/merchants", json=_onboard(billingPeriod="annual", contactEmail="b@test.co"))
    assert m.data.tables["payments"][-1]["amount_usd"] == 219.99
    assert m.data.tables["subscriptions"][-1]["billing_period"] == "annual"


def test_onboard_card_needs_no_proof_and_no_proof_activity(m):
    r = m.c.post("/merchants", json=_onboard(payment={"method": "card"}))
    assert r.status_code == 201
    assert m.data.tables["payments"][-1]["proof_storage_key"] is None
    assert [a["action"] for a in m.data.tables["merchant_activity"]] == ["signed_up"]


def test_onboard_requires_a_receipt_for_transfer(m):
    r = m.c.post("/merchants", json=_onboard(payment={"method": "manual_transfer"}))
    assert r.status_code == 422
    assert len(m.data.tables["merchants"]) == 1            # nothing was written


def test_onboard_rolls_back_the_tenant_when_the_login_cannot_be_created(m):
    def boom(**kw):
        raise Conflict("email already in use")
    m.mp.setattr(m.h, "provision_user", boom)
    r = m.c.post("/merchants", json=_onboard())
    assert r.status_code == 409
    assert [x["id"] for x in m.data.tables["merchants"]] == [M1]      # only the fixture one


def test_onboard_rolls_back_when_the_ledger_write_fails(m):
    m.data.fail_inserts.add("payments")
    with pytest.raises(RuntimeError):
        m.c.post("/merchants", json=_onboard())
    assert [x["id"] for x in m.data.tables["merchants"]] == [M1]


# --------------------------------------------------------------------------- #
# C. Locations: branch limit by plan, stats
# --------------------------------------------------------------------------- #
def _new_loc(m, **kw):
    return m.c.post(f"/merchants/{M1}/locations", json={
        "name": "Otro", "latitude": -0.2, "longitude": -78.5, **kw})


def test_starter_allows_one_active_branch_and_answers_402_for_the_second(m):
    r = _new_loc(m)
    assert r.status_code == 402
    err = r.json()
    assert err["code"] == "tier_limit"
    assert err["details"] == {"reason": "location_limit", "limit": 1, "requiredTier": "growth"}
    assert m.synced == []                                   # nothing changed, nothing re-synced


def test_an_inactive_branch_does_not_count_against_the_limit(m):
    r = _new_loc(m, active=False)
    assert r.status_code == 201 and r.json()["active"] is False
    assert m.synced == [M1]                                 # wallet re-sync still happens


def test_reactivating_a_branch_is_gated_by_the_limit(m):
    lid = _new_loc(m, active=False).json()["id"]
    r = m.c.patch(f"/merchants/{M1}/locations/{lid}", json={"active": True})
    assert r.status_code == 402 and r.json()["details"]["reason"] == "location_limit"
    # editing other fields of the inactive branch is fine
    assert m.c.patch(f"/merchants/{M1}/locations/{lid}", json={"name": "Z"}).status_code == 200
    # pausing the first branch frees the slot
    m.c.patch(f"/merchants/{M1}/locations/{LA}", json={"active": False})
    assert m.c.patch(f"/merchants/{M1}/locations/{lid}",
                     json={"active": True}).status_code == 200


def test_growth_allows_three_and_pro_ten_without_the_old_cap(m):
    tier(m, "growth")
    assert _new_loc(m).status_code == 201 and _new_loc(m).status_code == 201
    r = _new_loc(m)
    assert r.status_code == 402
    assert r.json()["details"] == {"reason": "location_limit", "limit": 3, "requiredTier": "pro"}
    tier(m, "pro")
    for _ in range(7):                                      # 3 -> 10
        assert _new_loc(m).status_code == 201
    r = _new_loc(m)
    assert r.status_code == 402
    assert r.json()["code"] == "tier_limit"
    assert r.json()["details"] == {"reason": "location_limit", "limit": 10}   # nothing above Pro


def test_location_list_carries_batched_stats(m):
    m.data.tables["merchant_locations"].append(_loc(LB, "Norte", n=1))
    m.data.tables["location_stats"] = [
        {"location_id": LA, "merchant_id": M1, "enrolled_clients": 42, "visits_30d": 130},
        {"location_id": LB, "merchant_id": M1, "enrolled_clients": 0, "visits_30d": 3},
        {"location_id": "zzz", "merchant_id": M2, "enrolled_clients": 9, "visits_30d": 9}]
    body = m.c.get(f"/merchants/{M1}/locations").json()
    assert [(x["id"], x["enrolledClients"], x["visits30d"]) for x in body] == [
        (LA, 42, 130), (LB, 0, 3)]
    # a branch without a stats row reads 0, and update returns the stats too
    m.data.tables["location_stats"].clear()
    assert m.c.get(f"/merchants/{M1}/locations").json()[0]["visits30d"] == 0
    tier(m, "growth")
    created = _new_loc(m).json()
    assert created["enrolledClients"] == 0 and created["visits30d"] == 0
    assert any(a["action"] == "location_added" for a in m.data.tables["merchant_activity"])


def test_locations_are_owner_only(m):
    _identity(m.h.app, m.mp, roles=("operation_user",))
    assert m.c.get(f"/merchants/{M1}/locations").status_code == 403


# --------------------------------------------------------------------------- #
# D. Team
# --------------------------------------------------------------------------- #
def _add_user(m, **kw):
    return m.c.post(f"/merchants/{M1}/operation-users", json={
        "email": "op@x.co", "station": "caja_1", "name": "Op", **kw})


def test_team_is_unlimited_and_defaults_to_cashier(m):
    for i in range(25):                                     # starter, far past the old limit of 2
        m.data.tables["profiles"].append(_profile(f"x{i}", name=f"U{i}"))
    r = _add_user(m)
    assert r.status_code == 201, r.text
    assert r.json()["staffRole"] == "cashier" and r.json()["tempPassword"] == "tmp-pass-1"
    assert m.provisioned[-1]["staff_role"] == "cashier"


def test_manager_role_is_a_pro_feature(m):
    r = _add_user(m, staffRole="manager")
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    assert r.json()["details"]["requiredTier"] == "pro"
    assert m.provisioned == []
    tier(m, "growth")
    assert _add_user(m, staffRole="manager").status_code == 402
    tier(m, "pro")
    r = _add_user(m, staffRole="manager")
    assert r.status_code == 201 and r.json()["staffRole"] == "manager"
    assert m.provisioned[-1]["staff_role"] == "manager"


def test_update_staff_role_writes_through_and_is_gated(m):
    m.data.tables["profiles"].append(_profile(CA, name="Ca"))
    calls = []
    m.mp.setattr(m.h, "update_user", lambda uid, **kw: calls.append((uid, kw)))
    r = m.c.patch(f"/merchants/{M1}/operation-users/{CA}", json={"staffRole": "manager"})
    assert r.status_code == 402 and calls == []
    tier(m, "pro")
    r = m.c.patch(f"/merchants/{M1}/operation-users/{CA}", json={"staffRole": "manager"})
    assert r.status_code == 200 and r.json()["staffRole"] == "manager"
    assert calls == [(CA, {"email": None, "station": None, "name": None,
                           "staff_role": "manager"})]
    # demoting to cashier is never gated
    tier(m, "starter")
    r = m.c.patch(f"/merchants/{M1}/operation-users/{CA}", json={"staffRole": "cashier"})
    assert r.status_code == 200 and r.json()["staffRole"] == "cashier"


def test_list_shows_role_and_last_sign_in_when_known(m):
    m.data.tables["profiles"] += [_profile(CA, name="Ca"), _profile(CB, name="Cb")]
    m.mp.setattr(m.h, "last_sign_ins", lambda ids: {CA: "2026-11-18T10:00:00+00:00"})
    body = {u["id"]: u for u in m.c.get(f"/merchants/{M1}/operation-users").json()}
    assert body[CA]["lastSignInAt"] == "2026-11-18T10:00:00+00:00"
    assert "lastSignInAt" not in body[CB] and body[CB]["staffRole"] == "cashier"


def test_list_survives_an_auth_failure(m):
    m.data.tables["profiles"].append(_profile(CA, name="Ca"))
    # the real helper degrades to {} when the auth client cannot be built (conftest blocks it)
    from conpass_common import provisioning
    m.mp.setattr(m.h, "last_sign_ins", provisioning.last_sign_ins)
    r = m.c.get(f"/merchants/{M1}/operation-users")
    assert r.status_code == 200 and "lastSignInAt" not in r.json()[0]


# --- manager scoping ------------------------------------------------------------------
@pytest.fixture
def team(m):
    """Pro merchant with branches A, B and C, a manager over A and B, and four cashiers."""
    tier(m, "pro")
    m.data.tables["merchant_locations"] += [_loc(LB, "Norte", n=1), _loc(LC, "Sur", n=2)]
    m.data.tables["profiles"] += [
        _profile(MGR, staff_role="manager", name="Mgr"),
        _profile(CA, name="Ca"), _profile(CB, name="Cb"), _profile(CAB, name="Cab"),
        _profile("cc", name="Cc"),                              # works in C only
        _profile("co", staff_role="manager", name="Other"),     # another manager
    ]
    m.data.tables["operation_user_locations"] += [
        {"user_id": MGR, "location_id": LA}, {"user_id": MGR, "location_id": LB},
        {"user_id": CA, "location_id": LA}, {"user_id": CB, "location_id": LB},
        {"user_id": CAB, "location_id": LA}, {"user_id": CAB, "location_id": LB},
        {"user_id": "cc", "location_id": LC}, {"user_id": "co", "location_id": LA},
    ]
    m.updates = []
    m.deleted = []
    m.resets = []
    m.mp.setattr(m.h, "update_user", lambda uid, **kw: m.updates.append((uid, kw)))
    m.mp.setattr(m.h, "delete_user", lambda uid: m.deleted.append(uid))
    m.mp.setattr(m.h, "reset_password", lambda uid: m.resets.append(uid) or "newpw")
    _identity(m.h.app, m.mp, roles=("operation_user",), user_id=MGR)
    return m


def test_manager_lists_only_cashiers_in_their_branches(team):
    ids = {u["id"] for u in team.c.get(f"/merchants/{M1}/operation-users").json()}
    assert ids == {CA, CB, CAB}              # not cc (branch C), not the managers


def test_manager_narrowed_to_one_branch(team):
    team.data.tables["operation_user_locations"] = [
        r for r in team.data.tables["operation_user_locations"]
        if not (r["user_id"] == MGR and r["location_id"] == LB)]
    ids = {u["id"] for u in team.c.get(f"/merchants/{M1}/operation-users").json()}
    assert ids == {CA, CAB}


def test_owner_sees_everyone_and_a_cashier_sees_nothing(team):
    _identity(team.h.app, team.mp)
    assert len(team.c.get(f"/merchants/{M1}/operation-users").json()) == 6
    _identity(team.h.app, team.mp, roles=("operation_user",), user_id=CA)
    assert team.c.get(f"/merchants/{M1}/operation-users").status_code == 403


def test_manager_demoted_or_downgraded_plan_loses_the_scope(team):
    tier(team, "growth")                                    # no longer Pro
    assert team.c.get(f"/merchants/{M1}/operation-users").status_code == 403
    tier(team, "pro")
    team.data.tables["profiles"][1]["staff_role"] = "cashier"   # the DB, not the token, decides
    assert team.c.get(f"/merchants/{M1}/operation-users").status_code == 403


def test_manager_creates_cashiers_in_scope_only(team):
    r = _add_user(team, locationIds=[LA])
    assert r.status_code == 201 and r.json()["staffRole"] == "cashier"
    assert team.provisioned[-1]["staff_role"] == "cashier"
    assert team.data.tables["operation_user_locations"][-1] == {
        "user_id": "new-1", "location_id": LA, "id": team.data.tables[
            "operation_user_locations"][-1]["id"], "created_at": team.data.tables[
            "operation_user_locations"][-1]["created_at"]}
    assert _add_user(team, locationIds=[LA, LC]).status_code == 403     # C is not theirs
    assert _add_user(team, locationIds=[]).status_code == 422           # must place the cashier
    assert _add_user(team).status_code == 422
    assert _add_user(team, staffRole="manager", locationIds=[LA]).status_code == 403
    assert len(team.provisioned) == 1


def test_manager_edits_only_cashiers_whose_branches_are_all_theirs(team):
    base = f"/merchants/{M1}/operation-users"
    assert team.c.patch(f"{base}/{CA}", json={"name": "Nueva"}).status_code == 200
    assert team.c.patch(f"{base}/{CAB}", json={"name": "Nueva"}).status_code == 200
    # outside: another branch, another manager, a cashier placed nowhere
    assert team.c.patch(f"{base}/cc", json={"name": "x"}).status_code == 403
    assert team.c.patch(f"{base}/co", json={"name": "x"}).status_code == 403
    team.data.tables["profiles"].append(_profile("nobody", name="Nobody"))
    assert team.c.patch(f"{base}/nobody", json={"name": "x"}).status_code == 403
    # a cashier in A and C is not "all inside" a manager of A and B
    team.data.tables["operation_user_locations"].append({"user_id": CA, "location_id": LC})
    assert team.c.patch(f"{base}/{CA}", json={"name": "x"}).status_code == 403


def test_manager_cannot_promote_or_move_a_cashier_out_of_scope(team):
    base = f"/merchants/{M1}/operation-users/{CA}"
    assert team.c.patch(base, json={"staffRole": "manager"}).status_code == 403
    assert team.c.patch(base, json={"locationIds": [LA, LC]}).status_code == 403
    assert team.c.patch(base, json={"locationIds": []}).status_code == 422
    assert team.c.patch(base, json={"locationIds": [LB]}).status_code == 200
    assert team.c.patch(base, json={"staffRole": "cashier"}).status_code == 200


def test_manager_resets_and_deletes_in_scope_only(team):
    base = f"/merchants/{M1}/operation-users"
    assert team.c.post(f"{base}/{CB}/reset-password").status_code == 200
    assert team.c.post(f"{base}/cc/reset-password").status_code == 403
    assert team.c.delete(f"{base}/{CB}").status_code == 204
    assert team.c.delete(f"{base}/cc").status_code == 403
    assert team.c.delete(f"{base}/co").status_code == 403
    assert team.resets == [CB] and team.deleted == [CB]
    assert CB not in [p["user_id"] for p in team.data.tables["profiles"]]


def test_manager_of_another_merchant_is_refused(team):
    _identity(team.h.app, team.mp, roles=("operation_user",), user_id=MGR, merchant_id=M2)
    assert team.c.get(f"/merchants/{M1}/operation-users").status_code == 403


def test_platform_admin_can_list_but_not_edit_the_team(team):
    _identity(team.h.app, team.mp, roles=("platform_admin",), merchant_id=None, user_id="adm")
    assert team.c.get(f"/merchants/{M1}/operation-users").status_code == 200
    assert team.c.patch(f"/merchants/{M1}/operation-users/{CA}",
                        json={"name": "x"}).status_code == 403


# --------------------------------------------------------------------------- #
# E. Payments, receipts and plan changes
# --------------------------------------------------------------------------- #
def _pay(pid, created, merchant=M1, key=None, **kw):
    return {"id": pid, "merchant_id": merchant, "kind": "renewal", "tier": "starter",
            "billing_period": "monthly", "amount_usd": 5.99, "method": "manual_transfer",
            "status": "confirmed", "proof_storage_key": key, "created_at": created, **kw}


def test_payments_newest_first_with_receipt_flag(m):
    m.data.tables["payments"] = [
        _pay("p1", "2026-09-01T00:00:00Z", key="k1"), _pay("p2", "2026-11-01T00:00:00Z"),
        _pay("p3", "2026-10-01T00:00:00Z", key="k3"), _pay("px", "2026-12-01T00:00:00Z",
                                                          merchant=M2)]
    body = m.c.get(f"/merchants/{M1}/payments").json()
    assert [p["id"] for p in body] == ["p2", "p3", "p1"]
    assert [p["hasReceipt"] for p in body] == [False, True, True]
    assert body[0]["amountUsd"] == 5.99 and body[0]["billingPeriod"] == "monthly"


def test_payments_are_owner_or_admin_only(m):
    _identity(m.h.app, m.mp, roles=("operation_user",))
    assert m.c.get(f"/merchants/{M1}/payments").status_code == 403
    _identity(m.h.app, m.mp, roles=("platform_admin",), merchant_id=None)
    assert m.c.get(f"/merchants/{M1}/payments").status_code == 200
    _identity(m.h.app, m.mp, merchant_id=M2)
    assert m.c.get(f"/merchants/{M1}/payments").status_code == 403


def test_receipt_is_presigned_and_scoped(m):
    m.data.tables["payments"] = [
        _pay("p1", "2026-09-01T00:00:00Z", key="k1"), _pay("p2", "2026-09-02T00:00:00Z"),
        _pay("px", "2026-09-03T00:00:00Z", merchant=M2, key="kx")]
    m.mp.setattr("conpass_common.assets.presign_payment_proof_download",
                 lambda key: f"https://s3.example/{key}?sig=1")
    r = m.c.get(f"/merchants/{M1}/payments/p1/receipt")
    assert r.status_code == 200 and r.json()["url"] == "https://s3.example/k1?sig=1"
    assert m.c.get(f"/merchants/{M1}/payments/p2/receipt").status_code == 404     # no receipt
    assert m.c.get(f"/merchants/{M1}/payments/px/receipt").status_code == 404     # not theirs
    assert m.c.get(f"/merchants/{M1}/payments/nope/receipt").status_code == 404
    _identity(m.h.app, m.mp, roles=("operation_user",))
    assert m.c.get(f"/merchants/{M1}/payments/p1/receipt").status_code == 403


def _request(m, tier_="growth", period="monthly"):
    return m.c.post(f"/merchants/{M1}/plan-change", json={"tier": tier_, "billingPeriod": period})


def test_upgrade_creates_a_pending_payment_and_emails_the_details(m):
    r = _request(m)
    assert r.status_code == 201, r.text
    b = r.json()
    assert (b["direction"], b["status"], b["amountUsd"]) == ("upgrade", "awaiting_payment", 17.99)
    assert b["fromTier"] == "starter" and b["toTier"] == "growth" and b["hasReceipt"] is False
    pay = m.data.tables["payments"][0]
    assert (pay["kind"], pay["status"], pay["tier"], pay["billing_period"]) == (
        "plan_change", "pending", "growth", "monthly")
    assert pay["amount_usd"] == 17.99
    assert m.data.tables["plan_change_requests"][0]["payment_id"] == pay["id"]
    to, subject, text = m.sender.sent[0]
    assert to == "owner@x.co" and "$17.99" in text and "IVA incluido" in text
    assert "2200112233" in text and "!" not in text
    assert m.data.tables["merchant_activity"][-1]["action"] == "plan_change_requested"


def test_upgrade_amount_follows_the_phase_and_period(m):
    m.data.tables["platform_settings"] = [{"id": True, "pricing_phase": "list"}]
    assert _request(m, "pro", "annual").json()["amountUsd"] == 718.99


def test_upgrade_survives_an_email_failure(m):
    m.mp.setattr(m.h, "get_email_sender", lambda: (_ for _ in ()).throw(RuntimeError("smtp")))
    assert _request(m).status_code == 201


def test_downgrade_is_scheduled_for_the_next_charge_date(m):
    tier(m, "pro")
    r = _request(m, "growth")
    b = r.json()
    assert (b["direction"], b["status"], b["effectiveAt"]) == ("downgrade", "scheduled",
                                                              "2026-12-05")
    assert "amountUsd" not in b and m.data.tables.get("payments", []) == []
    assert m.sender.sent == []


def test_downgrade_without_a_charge_date_lands_on_the_first_of_next_month(m):
    tier(m, "pro")
    m.data.tables["subscriptions"][0]["next_charge_at"] = None
    assert _request(m, "growth").json()["effectiveAt"] == "2026-12-01"


@pytest.mark.parametrize("frm,to,direction", [
    (("growth", "annual"), ("pro", "monthly"), "upgrade"),
    (("pro", "monthly"), ("growth", "annual"), "downgrade"),
    (("growth", "monthly"), ("growth", "annual"), "upgrade"),
    (("growth", "annual"), ("growth", "monthly"), "downgrade"),
])
def test_direction_through_the_endpoint(m, frm, to, direction):
    tier(m, frm[0])
    m.data.tables["subscriptions"][0]["billing_period"] = frm[1]
    assert _request(m, *to).json()["direction"] == direction


def test_same_plan_and_an_open_request_are_conflicts(m):
    r = _request(m, "starter", "monthly")
    assert r.status_code == 409 and r.json()["details"]["reason"] == "plan_change_same_plan"
    assert _request(m).status_code == 201
    r = _request(m, "pro")
    assert r.status_code == 409 and r.json()["details"]["reason"] == "plan_change_open"
    # a closed request no longer blocks
    m.data.tables["plan_change_requests"][0]["status"] = "rejected"
    assert _request(m, "pro").status_code == 201


def test_a_scheduled_downgrade_also_blocks_a_new_request(m):
    tier(m, "pro")
    assert _request(m, "growth").status_code == 201
    assert _request(m, "starter").json()["details"]["reason"] == "plan_change_open"


def test_plan_change_is_owner_only_and_not_for_the_demo(m):
    _identity(m.h.app, m.mp, roles=("platform_admin",), merchant_id=None)
    assert _request(m).status_code == 403
    _identity(m.h.app, m.mp, roles=("operation_user",))
    assert _request(m).status_code == 403
    _identity(m.h.app, m.mp)
    m.data.tables["merchants"][0]["is_demo"] = True
    assert _request(m).status_code == 403
    assert m.data.tables.get("plan_change_requests", []) == []


def test_list_shows_the_latest_ten_with_receipt_flags(m):
    reqs, pays = [], []
    for i in range(12):
        reqs.append({"id": f"r{i:02d}", "merchant_id": M1, "from_tier": "starter",
                     "to_tier": "growth", "from_period": "monthly", "to_period": "monthly",
                     "direction": "upgrade", "status": "applied", "payment_id": f"p{i:02d}",
                     "created_at": f"2026-10-{i + 1:02d}T00:00:00Z"})
        pays.append(_pay(f"p{i:02d}", "2026-10-01T00:00:00Z", key="k" if i == 11 else None))
    m.data.tables["plan_change_requests"], m.data.tables["payments"] = reqs, pays
    body = m.c.get(f"/merchants/{M1}/plan-change").json()
    assert len(body) == 10 and body[0]["id"] == "r11" and body[-1]["id"] == "r02"
    assert body[0]["hasReceipt"] is True and body[1]["hasReceipt"] is False


def test_cancel_rejects_the_payment_as_cancelled(m):
    rid = _request(m).json()["id"]
    assert m.c.delete(f"/merchants/{M1}/plan-change/{rid}").status_code == 204
    req = m.data.tables["plan_change_requests"][0]
    assert req["status"] == "cancelled" and req["resolved_at"]
    pay = m.data.tables["payments"][0]
    assert pay["status"] == "rejected" and pay["rejection_reason"] == "cancelled"
    # closed now: cancelling again conflicts, and a new request is possible
    assert m.c.delete(f"/merchants/{M1}/plan-change/{rid}").status_code == 409
    assert _request(m).status_code == 201


def test_cancel_a_scheduled_downgrade(m):
    tier(m, "pro")
    rid = _request(m, "growth").json()["id"]
    assert m.c.delete(f"/merchants/{M1}/plan-change/{rid}").status_code == 204
    assert m.data.tables["plan_change_requests"][0]["status"] == "cancelled"


def test_cancel_is_scoped_to_the_merchant(m):
    rid = _request(m).json()["id"]
    _identity(m.h.app, m.mp, merchant_id=M2)
    assert m.c.delete(f"/merchants/{M2}/plan-change/{rid}").status_code == 404


def test_proof_attaches_the_receipt_and_moves_to_review(m):
    rid = _request(m).json()["id"]
    r = m.c.post(f"/merchants/{M1}/plan-change/{rid}/proof", json={
        "method": "deuna", "proofStorageKey": "payment-proofs/z.png"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_review" and r.json()["hasReceipt"] is True
    pay = m.data.tables["payments"][0]
    assert pay["proof_storage_key"] == "payment-proofs/z.png" and pay["method"] == "deuna"
    assert m.data.tables["merchant_activity"][-1]["action"] == "proof_uploaded"
    # no longer awaiting payment
    again = m.c.post(f"/merchants/{M1}/plan-change/{rid}/proof", json={
        "method": "deuna", "proofStorageKey": "payment-proofs/z2.png"})
    assert again.status_code == 409


def test_proof_needs_a_receipt_for_transfer_and_deuna(m):
    rid = _request(m).json()["id"]
    for method in ("manual_transfer", "deuna"):
        r = m.c.post(f"/merchants/{M1}/plan-change/{rid}/proof", json={"method": method})
        assert r.status_code == 422
    assert m.data.tables["plan_change_requests"][0]["status"] == "awaiting_payment"


def test_proof_on_a_scheduled_downgrade_conflicts(m):
    tier(m, "pro")
    rid = _request(m, "growth").json()["id"]
    r = m.c.post(f"/merchants/{M1}/plan-change/{rid}/proof", json={
        "method": "manual_transfer", "proofStorageKey": "k"})
    assert r.status_code == 409
