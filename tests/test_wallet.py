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
        rewards_available=1, reward_text="Café gratis", accent_color="#0EA5E9",
        member_email="maria@example.com",
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
    # The card names itself in the kicker and the programme is the title; the balance is
    # no longer squeezed into the title because it has its own card-front row now (D14).
    assert obj["subheader"]["defaultValue"]["value"] == "Tarjeta de lealtad"
    assert obj["header"]["defaultValue"]["value"] == "Café"
    mods = {m["id"]: (m["header"], m["body"]) for m in obj["textModulesData"]}
    assert mods["hdr0"] == ("SELLOS", "●●●○○○○○")
    assert mods["hdr1"] == ("PROGRESO", "3 / 8")   # the figure, for reading at a glance
    assert mods["hdr2"] == ("PREMIOS", "1")
    assert mods["sec0"] == ("RECOMPENSA", "Café gratis")
    assert mods["sec1"] == ("MIEMBRO", "Juan R.")
    assert mods["sec2"] == ("CORREO", "maria@example.com")


def test_stamp_row_without_a_target(provider):
    """No target means no progress to draw — the count stands alone and the goal cell,
    which the template still references, says so rather than rendering blank."""
    mods = {m["id"]: (m["header"], m["body"])
            for m in provider._object_payload(
                _content(stamps=3, stamps_for_reward=None))["textModulesData"]}
    assert mods["hdr0"] == ("SELLOS", "3")
    assert mods["hdr1"] == ("META", "—")


def test_stamp_strip_collapses_when_the_target_is_too_large(provider):
    """Past a dozen the dots blur together, so the strip gives way to the figures."""
    from conpass_common.providers.google_wallet import MAX_STAMP_DOTS
    mods = {m["id"]: m["body"] for m in provider._object_payload(
        _content(stamps=5, stamps_for_reward=MAX_STAMP_DOTS + 1))["textModulesData"]}
    assert mods["hdr0"] == "5"
    assert mods["hdr1"] == str(MAX_STAMP_DOTS + 1)


def test_missing_card_level_values_render_a_placeholder(provider):
    """Name and email are per-CARD, so the class template cannot drop their cells for an
    anonymous enrollment — the object fills them instead of leaving them blank."""
    mods = {m["id"]: m["body"] for m in provider._object_payload(
        _content(holder_name=None, member_email=None))["textModulesData"]}
    assert mods["sec1"] == "—" and mods["sec2"] == "—"


def test_object_payload_points_and_membership(provider):
    pts = {m["id"]: (m["header"], m["body"]) for m in provider._object_payload(_content(
        program_type="loyalty_points", points=120, points_for_reward=200,
        stamps=0, stamps_for_reward=None))["textModulesData"]}
    assert pts["hdr0"] == ("PUNTOS", "120")
    assert pts["hdr1"] == ("META", "200")
    assert pts["hdr2"] == ("PREMIOS", "1")

    mem_obj = provider._object_payload(_content(
        program_type="membership_pass", membership_active_until="2026-12-31",
        membership_includes="Acceso al gimnasio", reward_text=None))
    assert mem_obj["subheader"]["defaultValue"]["value"] == "Pase de membresía"
    mem = {m["id"]: (m["header"], m["body"]) for m in mem_obj["textModulesData"]}
    assert mem["hdr0"] == ("ESTADO", "Activa")
    assert mem["hdr1"] == ("VÁLIDA HASTA", "2026-12-31")
    assert mem["hdr2"] == ("INCLUYE", "Acceso al gimnasio")

    open_ended = {m["id"]: m["body"] for m in provider._object_payload(_content(
        program_type="membership_pass", membership_active_until=None,
        reward_text=None))["textModulesData"]}
    assert open_ended["hdr1"] == "Sin caducidad"


def test_labels_follow_the_card_language(provider):
    """The language the customer had selected when they enrolled (D16), not the device's."""
    obj = provider._object_payload(_content(language="en"))
    assert obj["subheader"]["defaultValue"]["value"] == "Stamp card"
    assert obj["header"]["defaultValue"]["language"] == "en"
    mods = {m["id"]: m["header"] for m in obj["textModulesData"]}
    assert mods["hdr0"] == "STAMPS" and mods["hdr1"] == "PROGRESS"
    assert mods["sec0"] == "PROGRAM REWARD" and mods["sec2"] == "EMAIL"
    # Anything unrecognised lands on Ecuador-first Spanish rather than empty labels.
    assert provider._object_payload(
        _content(language="pt-BR"))["subheader"]["defaultValue"]["value"] == "Tarjeta de lealtad"


# --- the card-front template (class-level) ----------------------------------
def _paths(row: dict) -> list[str]:
    kind, items = next(iter(row.items()))
    order = {"oneItem": ["item"], "twoItems": ["startItem", "endItem"],
             "threeItems": ["startItem", "middleItem", "endItem"]}[kind]
    return [items[k]["firstValue"]["fields"][0]["fieldPath"] for k in order]


def test_class_template_draws_the_rows_on_the_card_front(provider):
    rows = (provider._class_payload(_content())
            ["classTemplateInfo"]["cardTemplateOverride"]["cardRowTemplateInfos"])
    assert [list(r)[0] for r in rows] == ["threeItems", "oneItem", "twoItems"]
    assert _paths(rows[0]) == [f"object.textModulesData['hdr{i}']" for i in range(3)]
    assert _paths(rows[1]) == ["object.textModulesData['sec0']"]
    assert _paths(rows[2]) == ["object.textModulesData['sec1']",
                               "object.textModulesData['sec2']"]


def test_class_template_drops_the_reward_row_when_the_program_has_none(provider):
    """Row shape is a PROGRAM decision, so a reward-less program simply has no such row —
    unlike the per-card cells, which cannot vary by class."""
    rows = (provider._class_payload(_content(reward_text=None))
            ["classTemplateInfo"]["cardTemplateOverride"]["cardRowTemplateInfos"])
    assert [list(r)[0] for r in rows] == ["threeItems", "twoItems"]
    assert _paths(rows[1]) == ["object.textModulesData['sec1']",
                               "object.textModulesData['sec2']"]


def test_ensure_class_updates_a_class_that_already_exists(provider, monkeypatch):
    """Create-only would strand every program whose class predates this layout — the
    card-front template is a class field, so it would keep rendering the old pass."""
    calls: list[tuple[str, str]] = []

    class _Resp:
        def __init__(self, code=200):
            self.status_code = code

        def raise_for_status(self):
            return None

    class _Api:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            calls.append(("get", url))
            return _Resp(self.code)

        def post(self, url, json):
            calls.append(("post", url))
            return _Resp()

        def put(self, url, json):
            calls.append(("put", url))
            return _Resp()

    class_url = f"/genericClass/{ISSUER}.program_{P1}"

    _Api.code = 200
    monkeypatch.setattr(provider, "_api", lambda: _Api())
    provider.sync_program(_content())
    assert calls == [("get", class_url), ("put", class_url)]

    calls.clear()
    _Api.code = 404
    provider.sync_program(_content())
    assert calls == [("get", class_url), ("post", "/genericClass")]


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


def test_object_payload_sends_background_color_alongside_hero_image(provider):
    # Google Wallet does NOT derive a pass background from the hero image — it falls back
    # to its own default — so a program that has both must send both: the colour paints
    # the pass, the hero image sits on it.
    obj = provider._object_payload(_content(
        accent_color="#112233", background_url="https://cdn.example/bg.jpg"))
    assert obj["hexBackgroundColor"] == "#112233"
    assert obj["heroImage"]["sourceUri"]["uri"] == "https://cdn.example/bg.jpg"


def test_object_payload_omits_background_color_when_unset(provider):
    # Only when the merchant set no colour at all — then Google uses its own default.
    obj = provider._object_payload(_content(
        accent_color=None, background_url="https://cdn.example/bg.jpg"))
    assert "hexBackgroundColor" not in obj


def test_update_replaces_the_whole_object(provider, monkeypatch):
    """update() must PUT, not PATCH: an appearance edit has to reach an installed pass,
    and only a full replace can clear an image the merchant removed."""
    calls: list[tuple[str, str, dict]] = []

    class _Resp:
        def raise_for_status(self):
            return None

    class _Api:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def put(self, url, json):
            calls.append(("put", url, json))
            return _Resp()

        def patch(self, url, json):  # pragma: no cover - must not be used
            calls.append(("patch", url, json))
            return _Resp()

    monkeypatch.setattr(provider, "_api", lambda: _Api())
    provider.update(_content(stamps=7, accent_color="#0EA5E9"))

    assert len(calls) == 1
    verb, url, body = calls[0]
    assert verb == "put"
    assert url == f"/genericObject/{ISSUER}.card_{CARD1}"
    mods = {m["id"]: m["body"] for m in body["textModulesData"]}
    assert mods["hdr0"] == "●●●●●●●○" and mods["hdr1"] == "7 / 8"
    assert body["hexBackgroundColor"] == "#0EA5E9"  # appearance travels with the update


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


def test_save_link_embeds_the_full_object(provider):
    """The JWT must carry the whole genericObject, not just its id: the link has to be
    able to create the pass on its own when the REST pre-creation didn't happen."""
    link = provider._save_link(_content())
    assert link.startswith("https://pay.google.com/gp/v/save/")
    token = link.rsplit("/", 1)[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["typ"] == "savetowallet"
    assert claims["aud"] == "google"
    # No `origins`: it only matters to the JS button API and costs ~200 encoded chars
    # of a budget that has to fit the whole object into an 1800-character URL.
    assert "origins" not in claims

    obj = claims["payload"]["genericObjects"][0]
    assert obj["id"] == f"{ISSUER}.card_{CARD1}"
    assert obj["classId"] == f"{ISSUER}.program_{P1}"
    assert obj["state"] == "ACTIVE"
    assert obj["barcode"] == {"type": "QR_CODE", "value": "TOK-abc"}
    assert obj["header"]["defaultValue"]["value"] == "Café"
    assert obj["cardTitle"]["defaultValue"]["value"] == "Conpass QA"
    assert obj["hexBackgroundColor"] == "#0EA5E9"
    # identical to what the REST call would have created — one payload builder, no drift
    assert obj == provider._object_payload(_content())


def test_save_link_without_images_embeds_and_fits(provider):
    link = provider._save_link(_content())
    from conpass_common.providers.google_wallet import SAVE_LINK_MAX_URL
    assert len(link) <= SAVE_LINK_MAX_URL
    claims = jwt.decode(link.rsplit("/", 1)[1], options={"verify_signature": False})
    assert claims["payload"]["genericObjects"][0]["barcode"]["value"] == "TOK-abc"


def test_link_references_the_object_whenever_it_is_known_to_exist(provider):
    """Google: over 1800 chars "the save may not work due to truncation by web browsers".
    A pass carrying images and the card-front rows is past that, so referencing an object
    we know exists is the PRIMARY path — that is what frees the layout to grow (D13)."""
    big = _content(
        logo_url="https://conpass-program-assets-prod.s3.us-east-1.amazonaws.com/"
                 "programs/aafe1128-8e59-4f5a-a8f8-7e589edd21d6/icon-"
                 "fad429da43164293b39a61362009aef2.png",
        background_url="https://conpass-program-assets-prod.s3.us-east-1.amazonaws.com/"
                       "programs/aafe1128-8e59-4f5a-a8f8-7e589edd21d6/background-"
                       "fad429da43164293b39a61362009aef2.jpg")
    from conpass_common.providers.google_wallet import SAVE_LINK_MAX_URL
    assert len(provider._save_link(big)) > SAVE_LINK_MAX_URL  # self-contained, oversized

    link = provider._save_link(big, object_exists=True)
    # …and the compact form is used even when the embedded one would have fitted.
    assert jwt.decode(provider._save_link(_content(), object_exists=True).rsplit("/", 1)[1],
                      options={"verify_signature": False}
                      )["payload"]["genericObjects"][0] == {"id": f"{ISSUER}.card_{CARD1}"}
    assert len(link) <= SAVE_LINK_MAX_URL
    obj = jwt.decode(link.rsplit("/", 1)[1],
                     options={"verify_signature": False})["payload"]["genericObjects"][0]
    assert obj == {"id": f"{ISSUER}.card_{CARD1}"}


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
