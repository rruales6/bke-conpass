"""Phase 17 — pinned demo campaigns: purge of visitor campaigns, 403/429 guards,
demoPinned/demoExpiresAt in responses, GET /demo program choice, notifier wiring."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conpass_common import demo
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from fastapi.testclient import TestClient

from tests.fakedb import FakeDB

M1 = "22222222-2222-2222-2222-222222222222"
M2 = "22222222-2222-2222-2222-222222222223"
U1 = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _prog(pid, *, merchant=M1, pinned=False, age=0, **kw):
    return {"id": pid, "merchant_id": merchant, "demo_pinned": pinned, "type": "loyalty_stamps",
            "name": pid, "mechanic": "stamps", "active": True, "created_at": iso(age), **kw}


def _db(programs, cards=()):
    return FakeDB(merchants=[{"id": M1, "is_demo": True}, {"id": M2, "is_demo": False}],
                  programs=programs, cards=list(cards))


# --------------------------------------------------------------------------- purge
def test_purge_deletes_only_old_unpinned_demo_programs():
    db = _db([_prog("pinned", pinned=True, age=999), _prog("young", age=29),
              _prog("old", age=31), _prog("other", merchant=M2, age=999)],
             cards=[{"id": "c1", "program_id": "old"}, {"id": "c2", "program_id": "young"}])
    revoked = []
    out = demo.purge_demo_campaigns(db, NOW, revoke=revoked.append)
    assert out == {"deleted": 1, "revoke_failed": 0}
    assert sorted(r["id"] for r in db.rows("programs")) == ["other", "pinned", "young"]
    assert revoked == ["c1"]


def test_purge_counts_revoke_failures_and_still_deletes():
    db = _db([_prog("old", age=60)], cards=[{"id": "c1", "program_id": "old"},
                                            {"id": "c2", "program_id": "old"}])

    def boom(card_id):
        raise RuntimeError("wallet down")

    out = demo.purge_demo_campaigns(db, NOW, revoke=boom)
    assert out == {"deleted": 1, "revoke_failed": 2}
    assert db.rows("programs") == []


def test_purge_noop_when_nothing_to_do():
    db = _db([_prog("pinned", pinned=True, age=999), _prog("young", age=1)])
    assert demo.purge_demo_campaigns(db, NOW, revoke=lambda c: 1 / 0) == {
        "deleted": 0, "revoke_failed": 0}
    assert len(db.rows("programs")) == 2


def test_expires_at_and_live_count():
    assert demo.demo_expires_at(_prog("p", pinned=True)) is None
    assert demo.demo_expires_at(_prog("p", age=0) | {"created_at": NOW.isoformat()}) == (
        NOW + timedelta(minutes=30))
    db = _db([_prog("a", age=5), _prog("b", age=29), _prog("c", age=45),
              _prog("d", pinned=True, age=1), _prog("e", merchant=M2, age=1)])
    assert demo.count_live_visitor_campaigns(db, M1, NOW) == 2


# --------------------------------------------------------------------------- programs
def _programs_client(monkeypatch, db):
    from services.programs import handler as h
    from services.programs.repository import ProgramsRepository

    monkeypatch.setattr(h, "get_repo", lambda: ProgramsRepository(db))
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], merchant_id=M1))
    return TestClient(h.app)


def test_patch_and_upload_url_forbidden_on_pinned(monkeypatch):
    c = _programs_client(monkeypatch, _db([_prog("pin", pinned=True)]))
    r = c.patch("/programs/pin", json={"name": "x"})
    assert r.status_code == 403 and r.json()["details"] == {"reason": "demo_pinned"}
    r = c.post("/programs/pin/appearance-upload-url",
               json={"kind": "icon", "contentType": "image/png"})
    assert r.status_code == 403 and r.json()["details"] == {"reason": "demo_pinned"}


def test_patch_allowed_on_visitor_campaign(monkeypatch):
    c = _programs_client(monkeypatch, _db([_prog("vis")]))
    r = c.patch("/programs/vis", json={"name": "Nuevo"})
    assert r.status_code == 200 and r.json()["name"] == "Nuevo"


def test_list_reports_pinned_and_expiry(monkeypatch):
    now = datetime.now(UTC)
    db = _db([_prog("pin", pinned=True), _prog("vis") | {"created_at": now.isoformat()}])
    out = {p["id"]: p for p in _programs_client(monkeypatch, db).get("/programs").json()}
    assert out["pin"]["demoPinned"] is True and "demoExpiresAt" not in out["pin"]
    assert "demoPinned" not in out["vis"]
    assert datetime.fromisoformat(out["vis"]["demoExpiresAt"]) == now + timedelta(minutes=30)


def test_non_demo_merchant_has_no_demo_fields(monkeypatch):
    db = _db([_prog("p", merchant=M2)])
    from services.programs import handler as h
    from services.programs.repository import ProgramsRepository
    monkeypatch.setattr(h, "get_repo", lambda: ProgramsRepository(db))
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], merchant_id=M2))
    p = TestClient(h.app).get("/programs").json()[0]
    assert "demoPinned" not in p and "demoExpiresAt" not in p


_CREATE = {"type": "loyalty_stamps", "name": "Mi tarjeta", "mechanic": "stamps",
           "stampsForReward": 8, "reward": "Gratis", "appearance": {}, "wallets": ["google"]}


def test_create_rate_limited_at_ten_live_visitor_campaigns(monkeypatch):
    now = datetime.now(UTC).isoformat()
    progs = [_prog(f"v{i}") | {"created_at": now} for i in range(demo.DEMO_MAX_VISITOR_CAMPAIGNS)]
    progs.append(_prog("pin", pinned=True))
    c = _programs_client(monkeypatch, _db(progs))
    r = c.post("/programs", json=_CREATE)
    assert r.status_code == 429
    assert r.json()["details"]["reason"] == "demo_campaign_limit"


def test_create_allowed_below_limit_and_returns_expiry(monkeypatch):
    now = datetime.now(UTC).isoformat()
    progs = [_prog(f"v{i}") | {"created_at": now} for i in range(9)]
    c = _programs_client(monkeypatch, _db(progs))
    r = c.post("/programs", json=_CREATE)
    assert r.status_code == 201, r.text
    assert "demoExpiresAt" in r.json()


# --------------------------------------------------------------------------- GET /demo
def _demo_program_id(monkeypatch, programs):
    from services.merchants import handler as h
    from services.merchants.repository import MerchantsRepository

    db = FakeDB(merchants=[{"id": M1, "business_name": "D", "is_demo": True,
                            "created_at": iso(0)}],
                programs=programs, profiles=[{"merchant_id": M1, "role": "merchant_owner",
                                              "email": "o@x.co"}])
    monkeypatch.setattr(h, "get_repo", lambda: MerchantsRepository(db))
    return TestClient(h.app).get("/demo").json()["programId"]


def test_demo_prefers_pinned_stamps_then_any_pinned_then_latest(monkeypatch):
    pts = _prog("pts", pinned=True, age=500, type="loyalty_points", mechanic="points")
    stamps = _prog("stamps", pinned=True, age=600)
    newest = _prog("newest", age=1)
    assert _demo_program_id(monkeypatch, [newest, pts, stamps]) == "stamps"
    assert _demo_program_id(monkeypatch, [newest, pts]) == "pts"
    assert _demo_program_id(monkeypatch, [newest, _prog("older", age=5)]) == "newest"


def test_ensure_sample_card_targets_pinned_stamps():
    db = _db([_prog("newest", age=1), _prog("stamps", pinned=True, age=600)])
    assert demo.ensure_sample_card(db, M1)["program_id"] == "stamps"


# --------------------------------------------------------------------------- notifier
def test_dispatch_survives_a_purge_failure(monkeypatch):
    from services.notifier import handler as n

    monkeypatch.setattr(n.dispatch, "run_dispatch", lambda c: {"sent": 1})

    def boom(c):
        raise RuntimeError("db down")

    monkeypatch.setattr(n.demo, "purge_demo_campaigns", boom)
    out = n.run_job("dispatch", "db")
    assert out["dispatch"] == {"sent": 1} and "error" in out["demo_purge"]
    monkeypatch.setattr(n.demo, "purge_demo_campaigns", lambda c: {"deleted": 2})
    assert n.run_job("dispatch", "db")["demo_purge"] == {"deleted": 2}
