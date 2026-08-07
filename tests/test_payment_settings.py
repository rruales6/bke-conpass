"""Phase 10 tests — platform payment settings + manual-transfer proof.

Covers: public GET /payment-settings (merchants), the public presigned proof
upload-url, onboarding's new proof-required validation, admin's partial-update
PATCH /admin/payment-settings, and the admin payment-proof lookup. All network is
mocked: fake repos + monkeypatched presign helpers, no Supabase or AWS access needed.
Uses monkeypatch so patches never leak across modules (see test_opusers_appearance.py
for the same pattern applied to the appearance-upload endpoint)."""
from __future__ import annotations

import conpass_common.payment_settings as payment_settings_mod
import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.errors import NotConfigured
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"


def _identity(app, monkeypatch, roles=("platform_admin",), merchant_id=None):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# Public GET /payment-settings + POST /payment-proofs/upload-url (merchants)
# --------------------------------------------------------------------------- #
class _FakeMerchantsRepo:
    def __init__(self, settings_row=None):
        self._settings_row = settings_row
        self.created_merchant: dict | None = None
        self.sub_calls: list[tuple] = []
        self.deleted: str | None = None

    def get_payment_settings(self):
        return self._settings_row

    def create_merchant(self, data):
        self.created_merchant = {**data, "id": M1, "created_at": "2026-08-01T00:00:00Z"}
        return self.created_merchant

    def create_subscription(self, merchant_id, tier, *,
                             payment_proof_key=None, payment_proof_uploaded_at=None):
        self.sub_calls.append((merchant_id, tier, payment_proof_key, payment_proof_uploaded_at))
        return {
            "merchant_id": merchant_id, "tier": tier, "payment_status": "pending",
            "mrr_usd": "49.00", "active_pass_limit": None, "program_limit": 3,
            "operation_user_limit": None, "next_charge_at": None, "last_payment_at": None,
            "payment_proof_key": payment_proof_key,
            "payment_proof_uploaded_at": payment_proof_uploaded_at,
        }

    def delete_merchant(self, merchant_id):
        self.deleted = merchant_id


def _seed_merchants(monkeypatch, settings_row=None):
    import services.merchants.handler as h
    repo = _FakeMerchantsRepo(settings_row)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    return h, repo


def test_payment_settings_not_configured_when_no_row(monkeypatch):
    h, _ = _seed_merchants(monkeypatch, settings_row=None)
    resp = TestClient(h.app).get("/payment-settings")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_payment_settings_configured_false_without_account_number(monkeypatch):
    h, _ = _seed_merchants(monkeypatch, settings_row={
        "bank_name": None, "account_type": None, "account_number": None,
        "beneficiary_name": None, "beneficiary_tax_id": None, "contact_email": None,
        "instructions": None, "qr_storage_key": None, "updated_at": None,
    })
    resp = TestClient(h.app).get("/payment-settings")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False
    assert resp.json()["qrImageUrl"] is None


def test_payment_settings_configured_true_derives_qr_url(monkeypatch):
    monkeypatch.setattr(payment_settings_mod, "public_asset_url",
                         lambda key: f"https://cdn.example/{key}" if key else None)
    h, _ = _seed_merchants(monkeypatch, settings_row={
        "bank_name": "Banco Pichincha", "account_type": "savings",
        "account_number": "123456", "beneficiary_name": "conpass SAS",
        "beneficiary_tax_id": "0999999999001", "contact_email": "pagos@conpass.cards",
        "instructions": "Enviar el comprobante", "qr_storage_key": "platform/payment-qr-abc.png",
        "updated_at": "2026-08-01T00:00:00Z",
    })
    resp = TestClient(h.app).get("/payment-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["qrImageUrl"] == "https://cdn.example/platform/payment-qr-abc.png"
    assert body["bankName"] == "Banco Pichincha"


def test_payment_proof_upload_url_happy_path(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)
    monkeypatch.setattr(h, "presign_payment_proof_upload", lambda **kw: {
        "uploadUrl": "https://s3.example/put?sig=1",
        "storageKey": "payment-proofs/abc.pdf",
    })
    resp = TestClient(h.app).post(
        "/payment-proofs/upload-url", json={"contentType": "application/pdf"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"uploadUrl": "https://s3.example/put?sig=1",
                     "storageKey": "payment-proofs/abc.pdf"}


def test_payment_proof_upload_url_unsupported_content_type(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)

    def _raise(**kw):
        raise ValueError(f"unsupported content type: {kw['content_type']}")
    monkeypatch.setattr(h, "presign_payment_proof_upload", _raise)
    resp = TestClient(h.app).post(
        "/payment-proofs/upload-url", json={"contentType": "image/png"})
    assert resp.status_code == 422


def test_payment_proof_upload_url_not_configured(monkeypatch):
    h, _ = _seed_merchants(monkeypatch)

    def _raise(**kw):
        raise RuntimeError("payment proofs bucket not configured")
    monkeypatch.setattr(h, "presign_payment_proof_upload", _raise)
    resp = TestClient(h.app).post(
        "/payment-proofs/upload-url", json={"contentType": "application/pdf"})
    assert resp.status_code == 503
    assert resp.json()["code"] == NotConfigured.code


# --------------------------------------------------------------------------- #
# POST /merchants onboarding — proof required for deuna / manual_transfer
# --------------------------------------------------------------------------- #
def _onboard_body(**payment):
    return {
        "businessName": "Café Test", "ruc": "0999999999001", "category": "cafe_restaurant",
        "city": "Quito", "contactName": "Ana", "contactEmail": "ana@test.co",
        "tier": "starter", "payment": payment,
    }


def _stub_onboard_side_effects(monkeypatch, h):
    import types
    monkeypatch.setattr(h, "get_payment_provider",
                         lambda: types.SimpleNamespace(submit=lambda intent: None))
    monkeypatch.setattr(h, "provision_user", lambda **kw: types.SimpleNamespace(
        user_id="owner-1", email=kw["email"], temp_password="tmp-pw-123"))


def test_onboard_rejects_manual_transfer_without_proof(monkeypatch):
    h, repo = _seed_merchants(monkeypatch)
    _stub_onboard_side_effects(monkeypatch, h)
    resp = TestClient(h.app).post(
        "/merchants", json=_onboard_body(method="manual_transfer"))
    assert resp.status_code == 422
    assert repo.created_merchant is None  # rejected before any writes


def test_onboard_rejects_deuna_without_proof(monkeypatch):
    h, repo = _seed_merchants(monkeypatch)
    _stub_onboard_side_effects(monkeypatch, h)
    resp = TestClient(h.app).post("/merchants", json=_onboard_body(method="deuna"))
    assert resp.status_code == 422
    assert repo.created_merchant is None


def test_onboard_accepts_and_persists_proof(monkeypatch):
    h, repo = _seed_merchants(monkeypatch)
    _stub_onboard_side_effects(monkeypatch, h)
    resp = TestClient(h.app).post("/merchants", json=_onboard_body(
        method="manual_transfer", proofStorageKey="payment-proofs/xyz.pdf"))
    assert resp.status_code == 201, resp.text
    assert repo.created_merchant is not None
    merchant_id, tier, proof_key, uploaded_at = repo.sub_calls[0]
    assert proof_key == "payment-proofs/xyz.pdf"
    assert uploaded_at is not None  # timestamp was stamped at write time


def test_onboard_card_payment_does_not_require_proof(monkeypatch):
    h, repo = _seed_merchants(monkeypatch)
    _stub_onboard_side_effects(monkeypatch, h)
    resp = TestClient(h.app).post("/merchants", json=_onboard_body(method="card"))
    assert resp.status_code == 201, resp.text
    _, _, proof_key, uploaded_at = repo.sub_calls[0]
    assert proof_key is None and uploaded_at is None


# --------------------------------------------------------------------------- #
# Admin — PATCH /admin/payment-settings (partial update) + payment-proof lookup
# --------------------------------------------------------------------------- #
class _FakeAdminRepo:
    def __init__(self, subscriptions=None, settings_row=None):
        self._subs = {s["merchant_id"]: s for s in (subscriptions or [])}
        self._settings_row = settings_row or {}
        self.last_patch: dict | None = None

    def get_subscription(self, merchant_id):
        return self._subs.get(merchant_id)

    def update_payment_settings(self, patch):
        self.last_patch = patch
        self._settings_row = {**self._settings_row, **patch}
        return self._settings_row


def _seed_admin(monkeypatch, subscriptions=None, settings_row=None):
    import services.admin.handler as h
    repo = _FakeAdminRepo(subscriptions, settings_row)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    return h, repo


def test_update_payment_settings_leaves_unset_fields_untouched(monkeypatch):
    h, repo = _seed_admin(monkeypatch, settings_row={
        "bank_name": "Banco Pichincha", "account_number": "123456",
    })
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch("/admin/payment-settings", json={"instructions": "Nuevo texto"})
    assert resp.status_code == 200, resp.text
    assert "bank_name" not in repo.last_patch
    assert "account_number" not in repo.last_patch
    assert repo.last_patch["instructions"] == "Nuevo texto"
    assert "updated_at" in repo.last_patch


def test_update_payment_settings_empty_string_clears_field(monkeypatch):
    h, repo = _seed_admin(monkeypatch, settings_row={
        "bank_name": "Banco Pichincha", "account_number": "123456",
    })
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch("/admin/payment-settings", json={"bankName": ""})
    assert resp.status_code == 200, resp.text
    assert repo.last_patch["bank_name"] == ""
    assert "account_number" not in repo.last_patch  # untouched — absent from the patch
    assert resp.json()["accountNumber"] == "123456"  # unaffected in the stored row


def test_update_payment_settings_requires_platform_admin(monkeypatch):
    h, _ = _seed_admin(monkeypatch, settings_row={})
    _identity(h.app, monkeypatch, roles=("merchant_owner",))
    resp = TestClient(h.app).patch("/admin/payment-settings", json={"bankName": "X"})
    assert resp.status_code == 403


def test_get_client_payment_proof_404_when_missing(monkeypatch):
    h, _ = _seed_admin(monkeypatch, subscriptions=[
        {"merchant_id": M1, "tier": "growth", "payment_status": "pending", "mrr_usd": "49.00"},
    ])
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).get(f"/admin/clients/{M1}/payment-proof")
    assert resp.status_code == 404


def test_get_client_payment_proof_returns_presigned_link(monkeypatch):
    h, _ = _seed_admin(monkeypatch, subscriptions=[
        {"merchant_id": M1, "tier": "growth", "payment_status": "pending", "mrr_usd": "49.00",
         "payment_proof_key": "payment-proofs/xyz.pdf",
         "payment_proof_uploaded_at": "2026-08-01T00:00:00Z"},
    ])
    monkeypatch.setattr(h, "presign_payment_proof_download",
                         lambda key: f"https://s3.example/get/{key}")
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).get(f"/admin/clients/{M1}/payment-proof")
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://s3.example/get/payment-proofs/xyz.pdf"
    assert body["uploadedAt"] == "2026-08-01T00:00:00Z"


@pytest.mark.parametrize("bucket_error", [RuntimeError("payment proofs bucket not configured")])
def test_qr_upload_url_not_configured_surfaces_as_503(monkeypatch, bucket_error):
    h, _ = _seed_admin(monkeypatch)

    def _raise(**kw):
        raise bucket_error
    monkeypatch.setattr(h, "presign_payment_qr_upload", _raise)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).post(
        "/admin/payment-settings/qr-upload-url", json={"contentType": "image/png"})
    assert resp.status_code == 503
