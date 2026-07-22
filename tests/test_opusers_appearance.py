"""Phase 8 tests — operation-user edit/delete/reset-password (merchants service) and
the program appearance presigned-upload endpoint (programs service). All network is
mocked: a tiny fake repo plus monkeypatched provisioning / presign helpers, so no
Supabase or AWS access is needed. Uses monkeypatch so patches never leak."""
from __future__ import annotations

from conpass_common.app import require_identity
from conpass_common.auth import Identity
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
OP = "44444444-4444-4444-4444-444444444444"
P1 = "33333333-3333-3333-3333-333333333333"


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# Operation-user management (merchants service)
# --------------------------------------------------------------------------- #
class _FakeMerchantsRepo:
    def __init__(self, op_user):
        self._op = op_user
        self.deleted_profile: tuple | None = None

    def get_operation_user(self, merchant_id, user_id):
        if self._op and self._op["user_id"] == user_id and self._op["merchant_id"] == merchant_id:
            return self._op
        return None

    def delete_operation_user_profile(self, merchant_id, user_id):
        self.deleted_profile = (merchant_id, user_id)


def _seed_merchants(monkeypatch, op_user=None):
    import services.merchants.handler as h
    op_user = op_user or {
        "user_id": OP, "merchant_id": M1, "role": "operation_user",
        "name": "Lucía Mora", "email": "lucia@x.co", "station": "caja_1",
    }
    repo = _FakeMerchantsRepo(op_user)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    return h, repo


def test_edit_operation_user_merges_fields(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)
    calls = {}
    monkeypatch.setattr(h, "update_user", lambda uid, **kw: calls.update({"uid": uid, **kw}))
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/merchants/{M1}/operation-users/{OP}", json={"station": "caja_2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"id": OP, "name": "Lucía Mora", "email": "lucia@x.co", "station": "caja_2"}
    assert calls["uid"] == OP and calls["station"] == "caja_2"


def test_edit_operation_user_not_found(monkeypatch):
    h, _ = _seed_merchants(monkeypatch, op_user={"user_id": "other", "merchant_id": M1})
    monkeypatch.setattr(h, "update_user", lambda *a, **k: None)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/merchants/{M1}/operation-users/{OP}", json={"name": "X"})
    assert resp.status_code == 404


def test_edit_operation_user_cross_tenant_forbidden(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)
    monkeypatch.setattr(h, "update_user", lambda *a, **k: None)
    _identity(h.app, monkeypatch, merchant_id=M2)  # caller belongs to a different merchant
    resp = TestClient(h.app).patch(
        f"/merchants/{M1}/operation-users/{OP}", json={"name": "X"})
    assert resp.status_code == 403


def test_edit_operation_user_requires_owner(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)
    monkeypatch.setattr(h, "update_user", lambda *a, **k: None)
    _identity(h.app, monkeypatch, roles=("operation_user",))
    resp = TestClient(h.app).patch(
        f"/merchants/{M1}/operation-users/{OP}", json={"name": "X"})
    assert resp.status_code == 403


def test_delete_operation_user(monkeypatch):
    h, repo = _seed_merchants(monkeypatch)
    deleted = {}
    monkeypatch.setattr(h, "delete_user", lambda uid: deleted.update({"uid": uid}))
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).delete(f"/merchants/{M1}/operation-users/{OP}")
    assert resp.status_code == 204
    assert deleted["uid"] == OP
    assert repo.deleted_profile == (M1, OP)  # profile row dropped (no auth cascade)


def test_reset_operation_user_password(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)
    monkeypatch.setattr(h, "reset_password", lambda uid: "fresh-temp-pw")
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(f"/merchants/{M1}/operation-users/{OP}/reset-password")
    assert resp.status_code == 200
    assert resp.json() == {"id": OP, "tempPassword": "fresh-temp-pw"}


# --------------------------------------------------------------------------- #
# Program appearance — presigned upload URL (programs service)
# --------------------------------------------------------------------------- #
class _FakeProgramsRepo:
    def __init__(self, program):
        self._p = program

    def get(self, program_id):
        return self._p if self._p and self._p["id"] == program_id else None


def _seed_programs(monkeypatch, program=None):
    import services.programs.handler as h
    program = program or {"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                          "name": "Club", "created_at": "2026-07-22T00:00:00Z"}
    repo = _FakeProgramsRepo(program)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    return h


def test_appearance_upload_url_returns_presigned_target(monkeypatch):
    h = _seed_programs(monkeypatch)
    monkeypatch.setattr(h, "presign_upload", lambda **kw: {
        "uploadUrl": "https://s3.example/put?sig=1",
        "storageKey": f"programs/{P1}/icon-abc.png",
        "publicUrl": f"https://bucket.s3/programs/{P1}/icon-abc.png",
    })
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        f"/programs/{P1}/appearance-upload-url",
        json={"kind": "icon", "contentType": "image/png"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["uploadUrl"].startswith("https://")
    assert body["storageKey"].endswith(".png")


def test_appearance_upload_url_program_not_found(monkeypatch):
    h = _seed_programs(monkeypatch)
    monkeypatch.setattr(h, "presign_upload", lambda **kw: {})
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        "/programs/00000000-0000-0000-0000-000000000000/appearance-upload-url",
        json={"kind": "icon", "contentType": "image/png"})
    assert resp.status_code == 404


def test_appearance_upload_url_cross_tenant_forbidden(monkeypatch):
    h = _seed_programs(monkeypatch)
    monkeypatch.setattr(h, "presign_upload", lambda **kw: {})
    _identity(h.app, monkeypatch, merchant_id=M2)
    resp = TestClient(h.app).post(
        f"/programs/{P1}/appearance-upload-url",
        json={"kind": "background", "contentType": "image/jpeg"})
    assert resp.status_code == 403
