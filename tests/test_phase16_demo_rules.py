"""Phase 16 — anti-spam rules of the public demo merchant (is_demo)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from conpass_common import demo
from conpass_common.notify import automations as au
from conpass_common.notify.settings import MerchantNotificationSettings

from tests.fakedb import FakeDB
from tests.test_phase16_notifications_engine import C1, M1, NOW, P1
from tests.test_phase16_notifications_service import M2, _svc


def iso(dt):
    return dt.astimezone(UTC).isoformat()


def _notif(i, status="sent", merchant=M1, source="manual", created=None):
    return {"id": f"n{i}", "merchant_id": merchant, "source": source, "status": status,
            "created_at": iso(created or datetime.now(UTC))}


BODY = {"type": "general", "audience": "all", "body": "Hola"}


def _client(monkeypatch, notifications=(), is_demo=True):
    c, db = _svc(monkeypatch, merchants=[{"id": M1, "is_demo": is_demo}],
                 notifications=list(notifications))
    return c, db


# --------------------------------------------------------------------------- #
# Manual sends: 3 / Ecuador day
# --------------------------------------------------------------------------- #
def test_demo_fourth_manual_send_is_rate_limited(monkeypatch):
    c, db = _client(monkeypatch, [_notif(i) for i in range(3)])
    r = c.post("/notifications", json=BODY)
    assert r.status_code == 429, r.text
    assert r.json()["details"] == {"reason": "demo_daily_limit", "limit": 3}
    assert len(db.rows("notifications")) == 3


def test_demo_allows_until_three_then_blocks(monkeypatch):
    c, db = _client(monkeypatch)
    for _ in range(3):
        assert c.post("/notifications", json=BODY).status_code == 202
    assert c.post("/notifications", json=BODY).status_code == 429
    assert len(db.rows("notifications")) == 3


def test_demo_cancelled_scheduled_and_queued_count(monkeypatch):
    c, db = _client(monkeypatch, [_notif(1, "cancelled"), _notif(2, "scheduled"),
                                  _notif(3, "queued")])
    assert c.post("/notifications", json=BODY).status_code == 429


def test_demo_ignores_other_merchant_automatic_and_yesterday(monkeypatch):
    now = datetime.now(UTC)
    c, db = _client(monkeypatch, [
        _notif(1, merchant=M2), _notif(2, source="automatic"),
        _notif(3, created=now - timedelta(days=2)), _notif(4, created=now - timedelta(days=2))])
    assert c.post("/notifications", json=BODY).status_code == 202


def test_non_demo_unaffected(monkeypatch):
    c, db = _client(monkeypatch, [_notif(i) for i in range(5)], is_demo=False)
    assert c.post("/notifications", json=BODY).status_code == 202


def test_manual_sends_today_uses_ecuador_day():
    # 2026-11-11 03:00 UTC is still 2026-11-10 22:00 in Ecuador.
    now = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
    db = FakeDB(notifications=[
        _notif(1, created=datetime(2026, 11, 11, 4, 59, tzinfo=UTC)),   # EC 23:59 Nov 10
        _notif(2, created=datetime(2026, 11, 10, 5, 0, tzinfo=UTC)),    # EC 00:00 Nov 10
        _notif(3, created=datetime(2026, 11, 10, 4, 59, tzinfo=UTC)),   # EC 23:59 Nov 9
        _notif(4, created=datetime(2026, 11, 11, 5, 0, tzinfo=UTC)),    # EC 00:00 Nov 11
    ])
    assert demo.manual_sends_today(db, M1, now) == 2
    after = datetime(2026, 11, 11, 5, 30, tzinfo=UTC)                  # EC Nov 11
    assert demo.manual_sends_today(db, M1, after) == 1


def test_is_demo_merchant():
    db = FakeDB(merchants=[{"id": M1, "is_demo": True}, {"id": M2, "is_demo": False}])
    assert demo.is_demo_merchant(db, M1) is True
    assert demo.is_demo_merchant(db, M2) is False
    assert demo.is_demo_merchant(db, "missing") is False


# --------------------------------------------------------------------------- #
# Automations
# --------------------------------------------------------------------------- #
def _event():
    return au.AccrualEvent(card_id=C1, merchant_id=M1, mechanic="stamps", before_held=7,
                           after_held=8, gained=1, target=8, rewards_earned=1,
                           accrual_count=1)


def _event_db(is_demo):
    return FakeDB(merchants=[{"id": M1, "is_demo": is_demo, "business_name": "Café"}],
                  cards=[{"id": C1, "merchant_id": M1, "program_id": P1, "language": "es"}],
                  programs=[{"id": P1, "reward": "Un café"}],
                  merchant_notification_settings=[])


def test_after_accrual_demo_enqueues_nothing():
    db = _event_db(True)
    assert au.after_accrual(db, _event()) is None
    assert db.rows("notification_outbox") == []


def test_after_accrual_non_demo_still_fires():
    db = _event_db(False)
    assert au.after_accrual(db, _event()) == "ready"


def _daily_db(is_demo):
    st = {"merchant_id": M1, **MerchantNotificationSettings(balance_expiry_days=30).as_row()}
    old = NOW - timedelta(days=31, hours=1)
    return FakeDB(
        merchant_notification_settings=[st],
        subscriptions=[{"merchant_id": M1, "tier": "pro"}],
        merchants=[{"id": M1, "is_demo": is_demo, "business_name": "Café"}],
        programs=[{"id": P1, "reward": "Un café"}],
        notifiable_cards=[{"card_id": C1, "merchant_id": M1, "program_id": P1,
                           "program_type": "loyalty_stamps", "stamps": 3, "points": 0,
                           "stamps_for_reward": 8, "points_for_reward": None}],
        card_last_activity=[{"card_id": C1, "merchant_id": M1, "last_at": old.isoformat()}],
        cards=[{"id": C1, "merchant_id": M1, "language": "es", "stamps": 3, "points": 0}])


def test_daily_skips_demo_merchant_entirely():
    db = _daily_db(True)
    totals = au.run_daily(db, NOW, push=lambda *a: None)
    assert totals["merchants"] == 0 and totals["expired"] == 0
    assert db.rows("cards")[0]["stamps"] == 3          # balance not zeroed
    assert db.rows("balance_expirations") == []


def test_daily_still_processes_non_demo_merchant():
    totals = au.run_daily(_daily_db(False), NOW, push=lambda *a: None)
    assert totals["expired"] == 1


# --------------------------------------------------------------------------- #
# Demo card expiry (daily job)
# --------------------------------------------------------------------------- #
def _cards_db():
    old = iso(NOW - timedelta(days=8))
    recent = iso(NOW - timedelta(days=6))
    return FakeDB(
        merchants=[{"id": M1, "is_demo": True}, {"id": M2, "is_demo": False}],
        cards=[
            {"id": "old", "merchant_id": M1, "active": True, "created_at": old},
            {"id": "old2", "merchant_id": M1, "active": True, "created_at": old},
            {"id": "new", "merchant_id": M1, "active": True, "created_at": recent},
            {"id": "sample", "merchant_id": M1, "active": True, "created_at": old,
             "dedupe_key": demo.DEMO_CARD_DEDUPE_KEY},
            {"id": "real", "merchant_id": M2, "active": True, "created_at": old}])


def _active(db):
    return {r["id"]: r["active"] for r in db.rows("cards")}


def test_expire_demo_cards_rules():
    db, revoked = _cards_db(), []
    out = demo.expire_demo_cards(db, NOW, revoke=revoked.append)
    assert _active(db) == {"old": False, "old2": False, "new": True, "sample": True,
                           "real": True}
    assert sorted(revoked) == ["old", "old2"]
    assert out == {"deactivated": 2, "revoke_failed": 0}


def test_expire_demo_cards_revoke_failure_does_not_stop():
    db = _cards_db()

    def boom(card_id):
        raise RuntimeError("404")

    out = demo.expire_demo_cards(db, NOW, revoke=boom)
    assert _active(db)["old"] is False and _active(db)["old2"] is False
    assert out == {"deactivated": 2, "revoke_failed": 2}


def test_revoke_object_id_uses_issuer(monkeypatch):
    from conpass_common import providers
    from conpass_common.config import settings

    calls = []
    monkeypatch.setattr(type(settings), "google_wallet_issuer_id", "ISS", raising=False)
    monkeypatch.setattr(providers, "get_wallet_provider",
                        lambda: type("P", (), {"revoke": lambda s, i: calls.append(i)})())
    demo._revoke_pass("abc")
    assert calls == ["ISS.card_abc"]


def test_notifier_daily_expires_demo_cards_first(monkeypatch):
    from services.notifier import handler as nh

    order = []
    monkeypatch.setattr(nh.demo, "expire_demo_cards",
                        lambda client: order.append("cards") or {"deactivated": 0})
    monkeypatch.setattr(nh.automations, "run_daily",
                        lambda client: order.append("daily") or {})
    monkeypatch.setattr(nh.dispatch, "run_dispatch",
                        lambda client: order.append("dispatch") or {})
    nh.run_job("daily", object())
    assert order == ["cards", "daily", "dispatch"]



def test_inactive_card_cannot_accrue_or_redeem_and_says_why(monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tests.test_api import CARD1, OP1, _seed_operations

    h, repo = _seed_operations(monkeypatch)
    repo.cards[CARD1].state = replace(repo.cards[CARD1].state, active=False,
                                      rewards_available=1)
    client = TestClient(h.app)
    for path, body in (("/operations/accrue", {"stamps": 1}), ("/operations/redeem", {})):
        r = client.post(path, headers={"Idempotency-Key": str(uuid.uuid4())},
                        json={"cardId": CARD1, "operationUserId": OP1, **body})
        assert r.status_code == 422 and r.json()["details"]["reason"] == "card_inactive"
