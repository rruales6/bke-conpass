"""Phase 4 (Google Wallet) tests — payload/save-link mapping and the three wired
endpoints (enrollment issue, cards wallet-links, operations resolve). All network is
mocked: the provider's REST calls are never exercised here (that path is covered by
the live integration test). Uses monkeypatch so patches never leak across modules."""
from __future__ import annotations

import json

import jwt
import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.providers.wallet import IssuedPass, PassContent, WalletKind
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
CARD1 = "44444444-4444-4444-4444-444444444444"
ISSUER = "3388000000000000000"


def _fake_sa() -> str:
    """A throwaway service-account JSON with a real RSA key so RS256 signing works."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps({
        "type": "service_account",
        "client_email": "qa@conpass-test.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    })


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", ISSUER)
    monkeypatch.setenv("GOOGLE_WALLET_SA_JSON", _fake_sa())
    from conpass_common.providers.google_wallet import GoogleWalletProvider
    return GoogleWalletProvider()


def _content(**over) -> PassContent:
    base = dict(
        card_id=CARD1, program_id=P1, merchant_id=M1,
        program_type="loyalty_stamps", program_name="Café", merchant_name="Conpass QA",
        holder_name="Juan R.", opaque_token="TOK-abc", stamps=3, stamps_for_reward=8,
        reward_text="Café gratis", accent_color="#0EA5E9",
    )
    base.update(over)
    return PassContent(**base)


# --- provider payload/id/link mapping (pure, no network) --------------------
def test_object_payload_maps_stamps_and_appearance(provider):
    obj = provider._object_payload(_content())
    assert obj["id"] == f"{ISSUER}.card_{CARD1}"
    assert obj["classId"] == f"{ISSUER}.program_{P1}"
    assert obj["state"] == "ACTIVE"
    assert obj["barcode"] == {"type": "QR_CODE", "value": "TOK-abc"}
    assert obj["hexBackgroundColor"] == "#0EA5E9"
    mods = {m["id"]: m["body"] for m in obj["textModulesData"]}
    assert mods["balance"] == "3 / 8"
    assert mods["reward"] == "Café gratis"


def test_object_payload_points_and_membership(provider):
    pts = provider._object_payload(_content(
        program_type="loyalty_points", points=120, points_for_reward=200,
        stamps=0, stamps_for_reward=None))
    assert {m["id"]: m["body"] for m in pts["textModulesData"]}["balance"] == "120 / 200"

    mem = provider._object_payload(_content(
        program_type="membership_pass", membership_active_until="2026-12-31",
        reward_text=None))
    mods = {m["id"]: m["body"] for m in mem["textModulesData"]}
    assert mods["validity"] == "2026-12-31"


def test_invalid_hex_color_is_dropped(provider):
    obj = provider._object_payload(_content(accent_color="teal"))
    assert "hexBackgroundColor" not in obj


def test_object_payload_maps_logo_and_hero_image(provider):
    obj = provider._object_payload(_content(
        logo_url="https://cdn.example/icon.png",
        background_url="https://cdn.example/bg.jpg"))
    assert obj["logo"]["sourceUri"]["uri"] == "https://cdn.example/icon.png"
    assert obj["heroImage"]["sourceUri"]["uri"] == "https://cdn.example/bg.jpg"


def test_object_payload_omits_images_when_absent(provider):
    obj = provider._object_payload(_content())
    assert "logo" not in obj and "heroImage" not in obj


def test_public_asset_url_resolves_key(monkeypatch):
    import conpass_common.assets as assets
    monkeypatch.setattr(type(assets.settings), "program_assets_base_url",
                        property(lambda self: "https://bucket.s3.us-east-1.amazonaws.com"))
    assert assets.public_asset_url("programs/p1/icon-x.png") == (
        "https://bucket.s3.us-east-1.amazonaws.com/programs/p1/icon-x.png")
    assert assets.public_asset_url(None) is None


def test_public_asset_url_none_when_unconfigured(monkeypatch):
    import conpass_common.assets as assets
    monkeypatch.setattr(type(assets.settings), "program_assets_base_url",
                        property(lambda self: None))
    assert assets.public_asset_url("programs/p1/icon-x.png") is None


def test_save_link_is_signed_savetowallet_jwt(provider):
    link = provider._save_link(f"{ISSUER}.card_{CARD1}")
    assert link.startswith("https://pay.google.com/gp/v/save/")
    token = link.rsplit("/", 1)[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["typ"] == "savetowallet"
    assert claims["aud"] == "google"
    assert claims["payload"]["genericObjects"][0]["id"] == f"{ISSUER}.card_{CARD1}"


def test_not_configured_when_secret_absent(monkeypatch):
    monkeypatch.delenv("GOOGLE_WALLET_ISSUER_ID", raising=False)
    monkeypatch.delenv("GOOGLE_WALLET_SA_JSON", raising=False)
    monkeypatch.setenv("CONPASS_SECRETS_FILE", "/nonexistent/secrets.yaml")
    from conpass_common.errors import NotConfigured
    from conpass_common.providers.google_wallet import GoogleWalletProvider
    with pytest.raises(NotConfigured):
        GoogleWalletProvider().add_link(_content())


# --- fakes for endpoint wiring ---------------------------------------------
class _FakeProvider:
    def __init__(self, link="https://pay.google.com/gp/v/save/FAKE", boom=False):
        self.link, self.boom, self.updated = link, boom, []

    def issue(self, content):
        if self.boom:
            raise RuntimeError("wallet down")
        return IssuedPass(WalletKind.GOOGLE, add_link=self.link,
                          provider_object_id="o", provider_class_id="c")

    def add_link(self, content):
        if self.boom:
            raise RuntimeError("wallet down")
        return self.link

    def update(self, content):
        self.updated.append(content.card_id)


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --- enrollment: best-effort issue -----------------------------------------
class _FakeEnrollRepo:
    program = {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Café",
               "mechanic": "stamps", "stamps_for_reward": 8, "reward": "Gratis",
               "welcome_bonus": 0, "active": True, "color": "#0EA5E9"}
    merchant = {"id": M1, "business_name": "Conpass QA"}

    def get_program(self, pid):
        return self.program

    def get_merchant(self, mid):
        return self.merchant

    def find_card_by_dedupe(self, pid, key):
        return None

    def create_customer(self, data):
        return {"id": "cust-1"}

    def create_card(self, data):
        return {**data, "id": CARD1, "created_at": "2026-07-18T00:00:00Z",
                "stamps": 0, "points": 0}


def _seed_enroll(monkeypatch, provider):
    from conpass_common.idempotency import InMemoryIdempotencyStore

    from services.enrollment import handler as h
    monkeypatch.setattr(h, "get_repo", lambda: _FakeEnrollRepo())
    monkeypatch.setattr(h, "get_idempotency_store", lambda: InMemoryIdempotencyStore())
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)
    return h


def test_enroll_fills_google_wallet_link(monkeypatch):
    h = _seed_enroll(monkeypatch, _FakeProvider())
    r = TestClient(h.app).post(f"/programs/{P1}/enroll", json={"fullName": "Juan Ruiz"},
                               headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    assert r.json()["walletLinks"]["google"] == "https://pay.google.com/gp/v/save/FAKE"


def test_enroll_survives_wallet_failure(monkeypatch):
    h = _seed_enroll(monkeypatch, _FakeProvider(boom=True))
    r = TestClient(h.app).post(f"/programs/{P1}/enroll", json={"fullName": "Juan Ruiz"},
                               headers={"Idempotency-Key": "k2"})
    assert r.status_code == 201                 # enrollment never fails on wallet error
    assert r.json()["walletLinks"] == {}


# --- cards: wallet-links ----------------------------------------------------
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, tables):
        self._t = tables

    def table(self, name):
        return _FakeQuery(self._t.get(name, []))


def test_cards_wallet_links(monkeypatch):
    from services.cards import handler as h
    tables = {
        "cards": [{"id": CARD1, "program_id": P1, "merchant_id": M1,
                   "type": "loyalty_stamps", "opaque_token": "TOK",
                   "created_at": "2026-07-18T00:00:00Z"}],
        "programs": [{"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                      "name": "Café", "stamps_for_reward": 8}],
        "merchants": [{"id": M1, "business_name": "Conpass QA"}],
    }
    monkeypatch.setattr(h, "service_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(h, "get_wallet_provider", lambda: _FakeProvider())
    _identity(h.app, monkeypatch)
    r = TestClient(h.app).get(f"/cards/{CARD1}/wallet-links")
    assert r.status_code == 200
    assert r.json() == {"google": "https://pay.google.com/gp/v/save/FAKE"}


# --- operations: resolve ----------------------------------------------------
def test_resolve_hydrates_card_and_program(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository
    repo = InMemoryRepository()
    repo.card_rows[CARD1] = {
        "id": CARD1, "program_id": P1, "merchant_id": M1, "type": "loyalty_stamps",
        "opaque_token": "TOK", "stamps": 3, "rewards_available": 0,
        "holder_name": "Juan R.", "active": True, "created_at": "2026-07-18T00:00:00Z"}
    repo.programs[P1] = {
        "id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Café",
        "mechanic": "stamps", "stamps_for_reward": 8, "wallets": ["google"],
        "welcome_bonus": 0, "active": True, "created_at": "2026-07-18T00:00:00Z"}
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _identity(h.app, monkeypatch, roles=("operation_user",))
    r = TestClient(h.app).post("/operations/resolve", json={"code": "TOK"})
    assert r.status_code == 200
    body = r.json()
    assert body["card"]["opaqueToken"] == "TOK"
    assert body["card"]["balance"]["stamps"] == 3
    assert body["program"]["name"] == "Café"
    assert body["program"]["wallets"] == ["google"]


def test_resolve_unknown_code_404(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository
    monkeypatch.setattr(h, "get_repo", lambda: InMemoryRepository())
    _identity(h.app, monkeypatch, roles=("operation_user",))
    r = TestClient(h.app).post("/operations/resolve", json={"code": "nope"})
    assert r.status_code == 404
