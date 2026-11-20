"""Phase 18 (D21) — password recovery by e-mailed code, GET /me console fields, and the
90-day card pause (card_lifecycle). Fakes only: FakeDB, a capturing e-mail sender and a
stubbed admin password write. No SMTP, no Supabase."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from conpass_common import card_lifecycle, password_reset, provisioning
from conpass_common import providers as providers_mod
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.demo import DEMO_OWNER_EMAIL
from conpass_common.email_reset import reset_code_email
from conpass_common.errors import ValidationFailed
from fastapi.testclient import TestClient

from services.identity import handler as h
from tests.fakedb import FakeDB

_REAL_COMPLETE = provisioning.complete_password_change
U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
EMAIL = "ana@tienda.ec"
GOOD_PW = "nueva-clave-9"


class Mailbox:
    configured = True

    def __init__(self):
        self.sent: list[tuple] = []

    def send(self, to, subject, text, html=None):
        self.sent.append((to, subject, text, html))

    def code(self, i=-1) -> str:
        return re.search(r"\b(\d{6})\b", self.sent[i][2]).group(1)


@pytest.fixture
def ctx(monkeypatch):
    db = FakeDB(profiles=[
        {"user_id": U1, "email": EMAIL, "name": "Ana Pérez", "merchant_id": M1,
         "staff_role": None},
        {"user_id": "u-demo", "email": DEMO_OWNER_EMAIL, "name": "Demo", "merchant_id": M1}])
    mail = Mailbox()
    changed: list[tuple] = []

    def complete(user_id, pw, *, email=None):
        changed.append((user_id, pw, email))

    monkeypatch.setattr(h, "service_client", lambda: db)
    monkeypatch.setattr(providers_mod, "get_email_sender", lambda: mail)
    monkeypatch.setattr(provisioning, "complete_password_change", complete)
    return type("Ctx", (), {"db": db, "mail": mail, "changed": changed,
                            "client": TestClient(h.app)})()


def _forgot(ctx, email=EMAIL, **extra):
    return ctx.client.post("/auth/forgot-password", json={"email": email, **extra})


def _reset(ctx, code, pw=GOOD_PW, email=EMAIL):
    return ctx.client.post("/auth/reset-password",
                           json={"email": email, "code": code, "newPassword": pw})


def _reason(r):
    return r.json()["details"]["reason"]


def _rows(ctx):
    return ctx.db.rows("password_reset_codes")


# --------------------------------------------------------------------------- #
# forgot-password
# --------------------------------------------------------------------------- #
def test_forgot_known_address_mails_a_six_digit_code(ctx):
    r = _forgot(ctx)
    assert r.status_code == 202
    assert len(ctx.mail.sent) == 1
    to, subject, text, html = ctx.mail.sent[0]
    code = ctx.mail.code()
    assert to == EMAIL and len(code) == 6 and code in html
    assert "!" not in subject + text and "contraseña" in subject      # tú, no exclamation
    row = _rows(ctx)[0]
    assert row["user_id"] == U1 and row.get("used_at") is None
    assert code not in row["code_hash"] and "$" in row["code_hash"]
    ttl = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    assert ttl == timedelta(minutes=15)


def test_forgot_english_email(ctx):
    _forgot(ctx, language="en")
    assert ctx.mail.sent[0][1].startswith("Your code")


def test_forgot_unknown_address_is_the_same_202_and_sends_nothing(ctx):
    r = _forgot(ctx, email="nadie@nowhere.ec")
    assert r.status_code == 202 and r.content == b""
    assert ctx.mail.sent == []
    spent = _rows(ctx)[0]
    assert spent.get("used_at") is not None                        # counts, never usable
    assert _reset(ctx, "000000", email="nadie@nowhere.ec").status_code == 422


def test_forgot_known_and_unknown_answer_identically(ctx):
    a, b = _forgot(ctx), _forgot(ctx, email="x@y.ec")
    assert (a.status_code, a.content, a.headers.get("content-type")) \
        == (b.status_code, b.content, b.headers.get("content-type"))


def test_forgot_demo_owner_never_gets_a_code(ctx):
    assert _forgot(ctx, email=DEMO_OWNER_EMAIL).status_code == 202
    assert ctx.mail.sent == []


def test_forgot_email_failure_still_202(ctx, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(ctx.mail, "send", boom)
    assert _forgot(ctx).status_code == 202


def test_forgot_rate_limit_is_three_per_fifteen_minutes(ctx):
    assert [_forgot(ctx).status_code for _ in range(3)] == [202, 202, 202]
    r = _forgot(ctx)
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited" and _reason(r) == "too_many_requests"
    assert len(ctx.mail.sent) == 3
    assert _forgot(ctx, email="otra@tienda.ec").status_code == 202    # per address


def test_forgot_rate_limit_counts_unknown_addresses_and_ignores_case(ctx):
    for _ in range(3):
        assert _forgot(ctx, email="ghost@x.ec").status_code == 202
    assert _forgot(ctx, email="GHOST@x.ec").status_code == 429


def test_forgot_rate_limit_window_expires(ctx):
    for _ in range(3):
        _forgot(ctx)
    for row in _rows(ctx):
        row["created_at"] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    assert _forgot(ctx).status_code == 202


def test_forgot_supersedes_the_previous_code(ctx):
    _forgot(ctx)
    old = ctx.mail.code()
    _forgot(ctx)
    new = ctx.mail.code()
    first, second = _rows(ctx)
    assert first.get("used_at") is not None and second.get("used_at") is None
    if old != new:
        r = _reset(ctx, old)
        assert r.status_code == 422 and _reason(r) == "invalid_code"
    assert _reset(ctx, new).status_code == 204


def test_forgot_validates_the_email(ctx):
    assert ctx.client.post("/auth/forgot-password", json={"email": "nope"}).status_code == 422


# --------------------------------------------------------------------------- #
# reset-password
# --------------------------------------------------------------------------- #
def test_reset_success_sets_password_marks_code_used(ctx):
    _forgot(ctx)
    r = _reset(ctx, ctx.mail.code())
    assert r.status_code == 204
    assert ctx.changed == [(U1, GOOD_PW, EMAIL)]
    assert all(row.get("used_at") for row in _rows(ctx))
    again = _reset(ctx, ctx.mail.code())
    assert again.status_code == 422 and _reason(again) == "expired_code"
    assert len(ctx.changed) == 1


def test_reset_email_is_case_insensitive(ctx):
    _forgot(ctx)
    assert _reset(ctx, ctx.mail.code(), email="ANA@Tienda.EC").status_code == 204


def test_reset_wrong_code_counts_an_attempt(ctx):
    _forgot(ctx)
    wrong = "000000" if ctx.mail.code() != "000000" else "111111"
    r = _reset(ctx, wrong)
    assert r.status_code == 422 and _reason(r) == "invalid_code"
    assert _rows(ctx)[0].get("attempts", 0) == 1
    assert ctx.changed == []
    assert _reset(ctx, ctx.mail.code()).status_code == 204     # the right one still works


def test_reset_fifth_wrong_attempt_burns_the_code(ctx):
    _forgot(ctx)
    good = ctx.mail.code()
    wrong = "000000" if good != "000000" else "111111"
    reasons = [_reason(_reset(ctx, wrong)) for _ in range(5)]
    assert reasons == ["invalid_code"] * 4 + ["too_many_attempts"]
    assert _rows(ctx)[0].get("attempts", 0) == 5 and _rows(ctx)[0].get("used_at")
    r = _reset(ctx, good)
    assert r.status_code == 422 and _reason(r) == "expired_code"
    assert ctx.changed == []


def test_reset_expired_code(ctx):
    _forgot(ctx)
    _rows(ctx)[0]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    r = _reset(ctx, ctx.mail.code())
    assert r.status_code == 422 and _reason(r) == "expired_code"


def test_reset_no_code_requested_is_expired_code(ctx):
    r = _reset(ctx, "123456")
    assert r.status_code == 422 and _reason(r) == "expired_code"


@pytest.mark.parametrize("pw", ["short", "1234567", ""])
def test_reset_weak_password_is_checked_first_and_costs_no_attempt(ctx, pw):
    _forgot(ctx)
    r = _reset(ctx, ctx.mail.code(), pw=pw)
    assert r.status_code == 422 and _reason(r) == "weak_password"
    assert _rows(ctx)[0].get("attempts", 0) == 0 and _rows(ctx)[0].get("used_at") is None


def test_reset_password_too_long_is_weak(ctx):
    _forgot(ctx)
    assert _reason(_reset(ctx, ctx.mail.code(), pw="x" * 73)) == "weak_password"


def test_reset_malformed_code_is_invalid_without_an_attempt(ctx):
    _forgot(ctx)
    for bad in ("12345", "abcdef", "1234567", "١٢٣٤٥٦"):
        r = _reset(ctx, bad)
        assert r.status_code == 422 and _reason(r) == "invalid_code"
    assert _rows(ctx)[0].get("attempts", 0) == 0


def test_reset_same_password_keeps_the_code_usable(ctx, monkeypatch):
    _forgot(ctx)
    code = ctx.mail.code()
    calls = []

    def complete(user_id, pw, *, email=None):
        calls.append(pw)
        if len(calls) == 1:
            raise ValidationFailed("same", details={"reason": "same_password"})
    monkeypatch.setattr(provisioning, "complete_password_change", complete)
    r = _reset(ctx, code, pw="mismo-clave-1")
    assert r.status_code == 422 and _reason(r) == "same_password"
    assert _rows(ctx)[0].get("used_at") is None
    assert _reset(ctx, code, pw="otra-clave-22").status_code == 204


def test_reset_weak_password_from_auth_server_passes_through(ctx, monkeypatch):
    _forgot(ctx)

    def complete(user_id, pw, *, email=None):
        raise ValidationFailed("weak", details={"reason": "weak_password"})
    monkeypatch.setattr(provisioning, "complete_password_change", complete)
    r = _reset(ctx, ctx.mail.code())
    assert _reason(r) == "weak_password" and _rows(ctx)[0].get("used_at") is None


def test_reset_clears_must_change_password_in_the_same_admin_write(ctx, monkeypatch):
    import types
    updates = []

    class Admin:
        def get_user_by_id(self, uid):
            meta = {"roles": ["merchant_owner"], "merchant_id": M1,
                    "must_change_password": True}
            return types.SimpleNamespace(user=types.SimpleNamespace(app_metadata=meta))

        def update_user_by_id(self, uid, attrs):
            updates.append((uid, attrs))

    client = types.SimpleNamespace(auth=types.SimpleNamespace(admin=Admin()))
    monkeypatch.setattr(provisioning, "complete_password_change", _REAL_COMPLETE)
    monkeypatch.setattr(provisioning, "service_client", lambda: client)
    monkeypatch.setattr(provisioning, "is_current_password", lambda *_a: False)
    _forgot(ctx)
    assert _reset(ctx, ctx.mail.code()).status_code == 204
    (uid, attrs), = updates
    assert uid == U1 and attrs["password"] == GOOD_PW
    assert attrs["app_metadata"]["must_change_password"] is False
    assert attrs["app_metadata"]["roles"] == ["merchant_owner"]


def test_code_compare_is_constant_time(ctx, monkeypatch):
    import hmac
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(password_reset.hmac, "compare_digest",
                        lambda a, b: seen.append(1) or real(a, b))
    _forgot(ctx)
    _reset(ctx, ctx.mail.code())
    assert seen


def test_code_helpers():
    stored = password_reset.hash_code("123456", "salty")
    assert stored.startswith("salty$") and "123456" not in stored
    assert password_reset.code_matches(stored, "123456")
    assert not password_reset.code_matches(stored, "123457")
    codes = {password_reset.new_code() for _ in range(50)}
    assert all(len(c) == 6 and c.isdigit() for c in codes) and len(codes) > 1


@pytest.mark.parametrize("lang", ["es", "en", None, "fr"])
def test_reset_email_copy(lang):
    subject, text, html = reset_code_email(lang, code="048213")
    assert "048213" in text and "048213" in html
    assert "!" not in subject + text and "15" in text
    if lang == "en":
        assert "password" in subject
    else:
        assert "contraseña" in subject and "Si no fuiste tú" in text


def test_reset_email_escapes_html():
    _, _, html = reset_code_email("es", code="<b>1</b>")
    assert "<b>1</b>" not in html


# --------------------------------------------------------------------------- #
# GET /me
# --------------------------------------------------------------------------- #
def _me(monkeypatch, db, *, roles=("merchant_owner",), merchant=M1, email=EMAIL):
    monkeypatch.setattr(h, "service_client", lambda: db)
    monkeypatch.setattr(h, "identity_locations", lambda *_a: [])
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email=email, roles=list(roles), merchant_id=merchant))
    return TestClient(h.app).get("/me").json()


def _me_db(**profile):
    return FakeDB(
        profiles=[{"user_id": U1, "name": "Ana Pérez", "staff_role": None, "merchant_id": M1,
                   "merchants": {"business_name": "Café Ana"}, **profile}],
        subscriptions=[{"merchant_id": M1, "tier": "growth"}])


def test_me_owner_has_name_business_and_tier(monkeypatch):
    body = _me(monkeypatch, _me_db())
    assert (body["name"], body["businessName"], body["tier"]) == ("Ana Pérez", "Café Ana", "growth")
    assert "staffRole" not in body
    assert body["userId"] == U1 and body["roles"] == ["merchant_owner"]
    assert body["mustChangePassword"] is False


def test_me_name_falls_back_to_email_local_part(monkeypatch):
    assert _me(monkeypatch, _me_db(name=None))["name"] == "ana"
    assert _me(monkeypatch, _me_db(name="  "))["name"] == "ana"


def test_me_operation_user_carries_staff_role(monkeypatch):
    body = _me(monkeypatch, _me_db(staff_role="manager"), roles=("operation_user",))
    assert body["staffRole"] == "manager" and body["tier"] == "growth"


def test_me_owner_never_reports_a_staff_role(monkeypatch):
    assert "staffRole" not in _me(monkeypatch, _me_db(staff_role="cashier"))


def test_me_platform_admin_has_no_business_or_tier(monkeypatch):
    db = FakeDB(profiles=[{"user_id": U1, "name": "Root", "merchant_id": None,
                           "merchants": None}])
    body = _me(monkeypatch, db, roles=("platform_admin",), merchant=None)
    assert body["name"] == "Root"
    assert "businessName" not in body and "tier" not in body


def test_me_embedded_merchant_as_list_and_missing_subscription(monkeypatch):
    db = _me_db(merchants=[{"business_name": "Café Ana"}])
    db.rows("subscriptions").clear()
    body = _me(monkeypatch, db)
    assert body["businessName"] == "Café Ana" and "tier" not in body


def test_me_degrades_when_the_lookup_fails(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(h, "identity_locations", lambda *_a: [])
    monkeypatch.setattr(h, "service_client", boom)
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email=EMAIL, roles=["merchant_owner"], merchant_id=M1))
    r = TestClient(h.app).get("/me")
    assert r.status_code == 200 and r.json()["name"] == "ana"


def test_me_uses_two_queries_at_most(monkeypatch):
    db = _me_db()
    seen = []
    real = db.table
    db.table = lambda name: seen.append(name) or real(name)
    _me(monkeypatch, db)
    assert seen == ["profiles", "subscriptions"]


# --------------------------------------------------------------------------- #
# card_lifecycle
# --------------------------------------------------------------------------- #
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _cards_db(last_at_days):
    cards, activity = [], []
    for i, (days, extra) in enumerate(last_at_days):
        cid = f"c{i}"
        cards.append({"id": cid, "active": True, "paused_at": None, **extra})
        activity.append({"card_id": cid,
                         "last_at": (NOW - timedelta(days=days)).isoformat()})
    return FakeDB(cards=cards, card_last_activity=activity)


def test_pause_inactive_cards_pauses_only_stale_active_unpaused():
    db = _cards_db([
        (91, {}),                                              # stale -> paused
        (89, {}),                                              # recent
        (90, {}),                                              # exactly 90 days: not yet older
        (200, {"active": False}),                              # deactivated: left alone
        (200, {"paused_at": "2026-05-01T00:00:00+00:00"}),     # already paused: not re-stamped
    ])
    assert card_lifecycle.pause_inactive_cards(db, now=NOW) == 1
    paused = {r["id"]: r["paused_at"] for r in db.rows("cards")}
    assert paused["c0"] == NOW.isoformat()
    assert paused["c1"] is None and paused["c2"] is None and paused["c3"] is None
    assert paused["c4"] == "2026-05-01T00:00:00+00:00"


def test_pause_inactive_cards_is_idempotent_and_keeps_the_balance():
    db = _cards_db([(120, {"stamps": 5, "points": 40, "rewards_available": 2})])
    assert card_lifecycle.pause_inactive_cards(db, now=NOW) == 1
    assert card_lifecycle.pause_inactive_cards(db, now=NOW + timedelta(days=1)) == 0
    row = db.rows("cards")[0]
    assert (row["stamps"], row["points"], row["rewards_available"]) == (5, 40, 2)
    assert row["paused_at"] == NOW.isoformat()


def test_pause_inactive_cards_batches_the_update():
    db = _cards_db([(100, {}) for _ in range(450)])
    updates = []
    real = db.table

    def table(name):
        q = real(name)
        if name == "cards":
            orig = q.update
            q.update = lambda patch: updates.append(1) or orig(patch)
        return q
    db.table = table
    assert card_lifecycle.pause_inactive_cards(db, now=NOW) == 450
    assert len(updates) == 3                                   # 200 + 200 + 50, not 450


def test_pause_inactive_cards_with_nothing_stale():
    assert card_lifecycle.pause_inactive_cards(_cards_db([(1, {})]), now=NOW) == 0


def test_reactivate_if_paused():
    db = FakeDB(cards=[{"id": "c1", "paused_at": "2026-01-01T00:00:00+00:00", "stamps": 4}])
    card = dict(db.rows("cards")[0])
    assert card_lifecycle.reactivate_if_paused(db, card) is True
    assert card["paused_at"] is None
    assert db.rows("cards")[0]["paused_at"] is None and db.rows("cards")[0]["stamps"] == 4
    assert card_lifecycle.reactivate_if_paused(db, card) is False
