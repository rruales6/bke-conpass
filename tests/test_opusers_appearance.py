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


# --------------------------------------------------------------------------- #
# Phase 11 — appearance writes: colour + image combine, empty string clears
# --------------------------------------------------------------------------- #
class _FakeProgramsCrudRepo:
    """Supports create/get/update so POST and PATCH /programs can be exercised."""

    def __init__(self, program=None):
        self._p = dict(program) if program else None
        self.created: dict | None = None
        self.updated_patch: dict | None = None

    def program_limit(self, merchant_id):
        return None

    def count_programs(self, merchant_id):
        return 0

    def create(self, data):
        self.created = data
        self._p = {**data, "id": P1, "created_at": "2026-08-13T00:00:00Z"}
        return self._p

    def get(self, program_id):
        return self._p if self._p and self._p["id"] == program_id else None

    def update(self, program_id, patch):
        self.updated_patch = patch
        self._p.update(patch)
        return self._p


def _seed_crud(monkeypatch, program=None):
    import services.programs.handler as h
    repo = _FakeProgramsCrudRepo(program)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    return h, repo


def _create_body(**appearance):
    return {
        "type": "loyalty_stamps", "name": "Club",
        "appearance": appearance,
        "wallets": ["apple"],
    }


def _program_row(**overrides):
    return {
        "id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Club",
        "created_at": "2026-07-22T00:00:00Z", "color": "#112233",
        "icon_storage_key": None, "background_storage_key": None, **overrides,
    }


def test_create_program_keeps_color_and_background_together(monkeypatch):
    # Google Wallet does NOT derive a background colour from the hero image, so the two
    # coexist: the colour is the pass background, the image is drawn on it.
    h, repo = _seed_crud(monkeypatch)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post("/programs", json=_create_body(
        color="#112233", backgroundStorageKey="programs/x/bg.png"))
    assert resp.status_code == 201
    ap = resp.json()["appearance"]
    assert ap["color"] == "#112233"
    assert ap["backgroundStorageKey"] == "programs/x/bg.png"


def test_create_program_color_only(monkeypatch):
    h, repo = _seed_crud(monkeypatch)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post("/programs", json=_create_body(color="#112233"))
    assert resp.status_code == 201
    assert resp.json()["appearance"]["color"] == "#112233"
    assert repo.created["background_storage_key"] is None


def test_create_program_background_only(monkeypatch):
    h, repo = _seed_crud(monkeypatch)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        "/programs", json=_create_body(backgroundStorageKey="programs/x/bg.png"))
    assert resp.status_code == 201
    assert resp.json()["appearance"]["backgroundStorageKey"] == "programs/x/bg.png"
    assert repo.created["color"] is None


def test_patch_program_adds_background_keeping_color(monkeypatch):
    h, repo = _seed_crud(monkeypatch, program=_program_row())
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(f"/programs/{P1}", json={
        "appearance": {"backgroundStorageKey": "programs/x/bg.png"}})
    assert resp.status_code == 200
    ap = resp.json()["appearance"]
    assert ap["color"] == "#112233"  # untouched — an omitted field is left alone
    assert ap["backgroundStorageKey"] == "programs/x/bg.png"


def test_patch_program_empty_string_clears_background(monkeypatch):
    # The panel's "remove image" path: "" writes NULL, an omitted field would not.
    h, repo = _seed_crud(monkeypatch, program=_program_row(
        background_storage_key="programs/x/bg.png"))
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(f"/programs/{P1}", json={
        "appearance": {"backgroundStorageKey": ""}})
    assert resp.status_code == 200
    assert repo.updated_patch["background_storage_key"] is None
    assert resp.json()["appearance"]["backgroundStorageKey"] is None
    assert resp.json()["appearance"]["color"] == "#112233"


def test_patch_program_empty_string_clears_color_and_icon(monkeypatch):
    h, repo = _seed_crud(monkeypatch, program=_program_row(
        icon_storage_key="programs/x/icon.png"))
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(f"/programs/{P1}", json={
        "appearance": {"color": "", "iconStorageKey": ""}})
    assert resp.status_code == 200
    assert repo.updated_patch["color"] is None
    assert repo.updated_patch["icon_storage_key"] is None


def test_patch_program_without_appearance_leaves_images_untouched(monkeypatch):
    h, repo = _seed_crud(monkeypatch, program=_program_row(
        name="Old Name", background_storage_key="programs/x/bg.png"))
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(f"/programs/{P1}", json={"name": "New Name"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert repo.updated_patch == {"name": "New Name"}


# --------------------------------------------------------------------------- #
# Phase 11 — a program edit reaches passes that are ALREADY installed
# --------------------------------------------------------------------------- #
class _FakePushRepo(_FakeProgramsCrudRepo):
    def __init__(self, program, cards):
        super().__init__(program)
        self._cards = cards

    def list_card_rows(self, program_id, limit):
        return self._cards[:limit]

    def count_cards(self, program_id):
        return len(self._cards)

    def get_merchant(self, merchant_id):
        return {"id": merchant_id, "business_name": "Café Vecino"}


def _seed_push(monkeypatch, program, cards):
    import services.programs.handler as h
    repo = _FakePushRepo(program, cards)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    pushed: list[str] = []

    class _Provider:
        def update(self, content):
            pushed.append(content.card_id)

    monkeypatch.setattr(h, "get_wallet_provider", lambda: _Provider())
    return h, repo, pushed


def _card(card_id):
    return {"id": card_id, "merchant_id": M1, "program_id": P1, "opaque_token": "TOK",
            "stamps": 2, "points": 0, "holder_name": "Juan"}


def test_appearance_edit_pushes_to_installed_passes(monkeypatch):
    h, repo, pushed = _seed_push(
        monkeypatch, _program_row(), [_card("c1"), _card("c2")])
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/programs/{P1}", json={"appearance": {"color": "#654321"}})
    assert resp.status_code == 200
    assert pushed == ["c1", "c2"]


def test_non_visible_edit_does_not_push(monkeypatch):
    # Toggling active changes nothing the pass shows — no reason to hit Google.
    h, repo, pushed = _seed_push(monkeypatch, _program_row(), [_card("c1")])
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(f"/programs/{P1}", json={"active": False})
    assert resp.status_code == 200
    assert pushed == []


def test_wallet_failure_never_fails_the_program_edit(monkeypatch):
    import services.programs.handler as h
    repo = _FakePushRepo(_program_row(), [_card("c1")])
    monkeypatch.setattr(h, "get_repo", lambda: repo)

    class _Boom:
        def update(self, content):
            raise RuntimeError("google is down")

    monkeypatch.setattr(h, "get_wallet_provider", lambda: _Boom())
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(f"/programs/{P1}", json={"name": "Nuevo nombre"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Nuevo nombre"


def test_push_is_bounded_by_the_card_cap(monkeypatch):
    import services.programs.handler as h
    cards = [_card(f"c{i}") for i in range(h.WALLET_PUSH_MAX_CARDS + 5)]
    h, repo, pushed = _seed_push(monkeypatch, _program_row(), cards)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/programs/{P1}", json={"appearance": {"color": "#654321"}})
    assert resp.status_code == 200
    assert len(pushed) == h.WALLET_PUSH_MAX_CARDS

