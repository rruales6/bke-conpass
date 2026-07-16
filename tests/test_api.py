"""API-level tests via FastAPI TestClient — health, identity, and the operations
accrue flow end-to-end with an in-memory repository (no DB, no network).

Uses pytest's monkeypatch so module-level patches auto-restore and never leak into
other test modules (e.g. the live integration test)."""
import uuid

from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.idempotency import InMemoryIdempotencyStore
from fastapi.testclient import TestClient

# Contract IDs are UUIDs; use fixed valid ones across the suite.
U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
CARD1 = "44444444-4444-4444-4444-444444444444"
OP1 = "55555555-5555-5555-5555-555555555555"


def test_health():
    from services.health.handler import app
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_me_returns_identity(monkeypatch):
    from services.identity.handler import app
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], merchant_id=M1))
    body = TestClient(app).get("/me").json()
    assert body["userId"] == U1
    assert body["roles"] == ["merchant_owner"]


def _seed_operations(monkeypatch, *, merchant_id=M1, roles=("operation_user",)):
    """Return (module, repo) with one stamps card ready to accrue. All patches are
    registered on `monkeypatch` so they auto-restore after the test."""
    from services.operations import handler as h
    from services.operations.logic import CardState, ProgramRules
    from services.operations.repository import CardRow, InMemoryRepository

    repo = InMemoryRepository()
    repo.rules[P1] = ProgramRules("loyalty_stamps", "stamps", 8, None, None)
    repo.cards[CARD1] = CardRow(
        id=CARD1, program_id=P1, merchant_id=M1, program_type="loyalty_stamps",
        state=CardState(stamps=7, points=0, rewards_available=0, active=True))

    store = InMemoryIdempotencyStore()
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: store)
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))
    return h, repo


def test_accrue_is_idempotent_and_authoritative(monkeypatch):
    h, repo = _seed_operations(monkeypatch)
    client = TestClient(h.app)
    key = str(uuid.uuid4())
    payload = {"cardId": CARD1, "operationUserId": OP1, "stamps": 3}
    headers = {"Idempotency-Key": key}

    r1 = client.post("/operations/accrue", json=payload, headers=headers)
    r2 = client.post("/operations/accrue", json=payload, headers=headers)  # replay

    assert r1.status_code == 200
    body = r1.json()
    assert body["card"]["balance"]["rewardsAvailable"] == 1   # 7+3 crosses 8
    assert body["card"]["balance"]["stamps"] == 2
    assert r1.json() == r2.json()                              # identical replay
    assert len(repo.txns) == 1                                 # applied exactly once


def test_accrue_requires_idempotency_key(monkeypatch):
    h, _ = _seed_operations(monkeypatch)
    resp = TestClient(h.app).post(
        "/operations/accrue",
        json={"cardId": CARD1, "operationUserId": OP1, "stamps": 1})
    assert resp.status_code == 422  # missing Idempotency-Key


def test_cross_tenant_accrue_forbidden(monkeypatch):
    other_merchant = "99999999-9999-9999-9999-999999999999"
    h, _ = _seed_operations(monkeypatch, merchant_id=other_merchant)
    resp = TestClient(h.app).post(
        "/operations/accrue",
        json={"cardId": CARD1, "operationUserId": OP1, "stamps": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())})
    assert resp.status_code == 403
