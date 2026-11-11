"""Phase 16 — admin notification rules, notification overage in admin estimates and the
wallet-class re-sync on plan change."""
from __future__ import annotations

import pytest
from _fake_supabase import FakeClient
from conpass_common import wallet_sync
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from fastapi.testclient import TestClient

M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"


def _as(app, monkeypatch, roles):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id="u", email="a@b.co", roles=list(roles)))


@pytest.fixture
def admin(monkeypatch):
    import services.admin.handler as h
    client = FakeClient({
        "platform_notification_rules": [
            {"id": True, "cap_per_day": 2, "quiet_hours_enabled": True}],
        "merchants": [{"id": M1, "business_name": "Uno", "city": "Quito",
                       "created_at": "2026-07-01T00:00:00Z"}],
        "subscriptions": [{"merchant_id": M1, "tier": "starter", "payment_status": "paid"}],
        "notification_deliveries": [],
    })
    monkeypatch.setattr(h, "get_repo", lambda: h.AdminRepository(client))
    _as(h.app, monkeypatch, ["platform_admin"])
    h.client = client
    return h


def test_get_rules(admin):
    r = TestClient(admin.app).get("/admin/notification-rules")
    assert r.status_code == 200
    assert r.json() == {"capPerDay": 2, "quietHoursEnabled": True, "quietStart": "21:00",
                        "quietEnd": "08:00", "timezone": "America/Guayaquil"}


def test_patch_rules_partial(admin):
    c = TestClient(admin.app)
    r = c.patch("/admin/notification-rules", json={"capPerDay": 3})
    assert r.status_code == 200 and r.json()["capPerDay"] == 3
    assert r.json()["quietHoursEnabled"] is True
    r = c.patch("/admin/notification-rules", json={"quietHoursEnabled": False})
    assert r.json() == {"capPerDay": 3, "quietHoursEnabled": False, "quietStart": "21:00",
                        "quietEnd": "08:00", "timezone": "America/Guayaquil"}
    assert c.patch("/admin/notification-rules", json={}).json()["capPerDay"] == 3


@pytest.mark.parametrize("body", [{"capPerDay": 0}, {"capPerDay": 4},
                                  {"capPerDay": "x"}, {"other": 1}])
def test_patch_rules_validation(admin, body):
    assert TestClient(admin.app).patch("/admin/notification-rules", json=body).status_code == 422


def test_rules_require_platform_admin(admin, monkeypatch):
    c = TestClient(admin.app)
    for role in ("merchant_owner", "operation_user"):
        _as(admin.app, monkeypatch, [role])
        assert c.get("/admin/notification-rules").status_code == 403
        assert c.patch("/admin/notification-rules", json={"capPerDay": 1}).status_code == 403


def test_clients_estimate_includes_notification_overage(admin, monkeypatch):
    from datetime import date
    monkeypatch.setattr(admin, "period_start", lambda today: date(2026, 11, 1))
    admin.client.tables["notification_deliveries"] = [
        {"id": str(i), "merchant_id": M1, "status": "sent",
         "sent_at": "2026-11-03T10:00:00+00:00"} for i in range(600)]   # 100 over 500
    body = TestClient(admin.app).get("/admin/clients").json()
    assert body[0]["estimatedMonthlyUsd"] == pytest.approx(4.99 + 100 * 0.015)
    stats = TestClient(admin.app).get("/admin/stats").json()
    assert stats["mrrUsd"] == pytest.approx(4.99 + 100 * 0.015)
    assert stats["subscriptionMrrUsd"] == pytest.approx(4.99)


def test_tier_change_syncs_classes(admin, monkeypatch):
    calls = []
    monkeypatch.setattr(wallet_sync, "sync_merchant_classes", lambda c, mid: calls.append(mid))
    c = TestClient(admin.app)
    assert c.patch(f"/admin/clients/{M1}/subscription", json={"tier": "growth"}).status_code == 200
    assert calls == [M1]
    # same tier / payment-only change: no sync
    c.patch(f"/admin/clients/{M1}/subscription", json={"tier": "growth"})
    c.patch(f"/admin/clients/{M1}/subscription", json={"paymentStatus": "overdue"})
    assert calls == [M1]


def test_tier_change_survives_sync_failure(admin, monkeypatch):
    def boom(*_a):
        raise RuntimeError("wallet down")
    monkeypatch.setattr(wallet_sync, "sync_merchant_classes", boom)
    r = TestClient(admin.app).patch(f"/admin/clients/{M1}/subscription", json={"tier": "pro"})
    assert r.status_code == 200 and r.json()["tier"] == "pro"
