"""admin service — platform-admin, cross-tenant (Función 04). Requires platform_admin."""
from datetime import UTC, datetime

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.assets import presign_payment_proof_download, presign_payment_qr_upload
from conpass_common.errors import NotConfigured, NotFound, ValidationFailed
from conpass_common.models import PaymentQrUploadRequest, PaymentSettingsUpdate, SubscriptionUpdate
from conpass_common.payment_settings import payment_settings_model

from .repository import AdminRepository

app = create_app(service="admin")

# PaymentSettingsUpdate (camelCase) -> platform_payment_settings column (snake_case).
_PAYMENT_SETTINGS_FIELDS = {
    "bankName": "bank_name",
    "accountType": "account_type",
    "accountNumber": "account_number",
    "beneficiaryName": "beneficiary_name",
    "beneficiaryTaxId": "beneficiary_tax_id",
    "contactEmail": "contact_email",
    "instructions": "instructions",
    "qrStorageKey": "qr_storage_key",
}


def get_repo() -> AdminRepository:
    return AdminRepository()


def _admin_client_model(m: dict, sub: dict | None) -> dict:
    out = {
        "merchantId": m["id"], "name": m["business_name"], "city": m.get("city"),
        "tier": sub["tier"] if sub else None,
        "paymentStatus": sub["payment_status"] if sub else None,
        "mrrUsd": float(sub["mrr_usd"]) if sub else 0.0,
        "since": m["created_at"],
    }
    if sub and sub.get("next_charge_at") is not None:
        out["nextChargeAt"] = sub["next_charge_at"]
    if sub and sub.get("last_payment_at") is not None:
        out["lastPaymentAt"] = sub["last_payment_at"]
    out["hasPaymentProof"] = bool(sub and sub.get("payment_proof_key"))
    if sub and sub.get("payment_proof_uploaded_at") is not None:
        out["paymentProofUploadedAt"] = sub["payment_proof_uploaded_at"]
    return out


def _subscription_model(sub: dict, active_pass_count: int) -> dict:
    return {
        "tier": sub["tier"],
        "paymentStatus": sub["payment_status"],
        "mrrUsd": float(sub["mrr_usd"]),
        "activePassLimit": sub.get("active_pass_limit"),
        "activePassCount": active_pass_count,
        "programLimit": sub.get("program_limit"),
        "operationUserLimit": sub.get("operation_user_limit"),
        "nextChargeAt": sub.get("next_charge_at"),
        "lastPaymentAt": sub.get("last_payment_at"),
    }


@app.get("/admin/clients")
def list_clients(identity: CurrentIdentity):
    identity.require_role("platform_admin")
    repo = get_repo()
    merchants = repo.list_merchants()
    subs_by_merchant = {s["merchant_id"]: s for s in repo.list_subscriptions()}
    return [_admin_client_model(m, subs_by_merchant.get(m["id"])) for m in merchants]


@app.patch("/admin/clients/{merchant_id}/subscription")
def update_subscription(merchant_id: str, body: SubscriptionUpdate, identity: CurrentIdentity):
    identity.require_role("platform_admin")
    repo = get_repo()
    existing = repo.get_subscription(merchant_id)
    if existing is None:
        raise NotFound("subscription not found")

    patch: dict = {}
    if body.tier is not None:
        patch["tier"] = body.tier.value
    if body.paymentStatus is not None:
        patch["payment_status"] = body.paymentStatus.value
    updated = repo.update_subscription(merchant_id, patch) if patch else existing

    active_pass_count = repo.active_pass_count(merchant_id)
    return _subscription_model(updated, active_pass_count)


@app.get("/admin/clients/{merchant_id}/payment-proof")
def get_client_payment_proof(merchant_id: str, identity: CurrentIdentity):
    identity.require_role("platform_admin")
    sub = get_repo().get_subscription(merchant_id)
    proof_key = sub.get("payment_proof_key") if sub else None
    if not proof_key:
        raise NotFound("this client has not uploaded a payment proof")
    return {
        "url": presign_payment_proof_download(proof_key),
        "uploadedAt": sub.get("payment_proof_uploaded_at"),
    }


@app.patch("/admin/payment-settings")
def update_payment_settings(body: PaymentSettingsUpdate, identity: CurrentIdentity):
    identity.require_role("platform_admin")
    # exclude_unset: only fields the caller actually sent are touched — an explicit ""
    # clears a field, an absent field is left as-is. mode="json" turns AccountType into
    # its plain string so it writes straight to the DB.
    sent = body.model_dump(exclude_unset=True, mode="json")
    patch = {_PAYMENT_SETTINGS_FIELDS[k]: v for k, v in sent.items()}
    patch["updated_at"] = datetime.now(UTC).isoformat()
    row = get_repo().update_payment_settings(patch)
    return payment_settings_model(row)


@app.post("/admin/payment-settings/qr-upload-url")
def create_payment_qr_upload_url(body: PaymentQrUploadRequest, identity: CurrentIdentity):
    identity.require_role("platform_admin")
    try:
        return presign_payment_qr_upload(content_type=body.contentType.value)
    except RuntimeError as exc:
        raise NotConfigured(str(exc)) from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@app.get("/admin/stats")
def get_platform_stats(identity: CurrentIdentity):
    identity.require_role("platform_admin")
    subs = get_repo().list_subscriptions()
    paid = [s for s in subs if s.get("payment_status") == "paid"]
    return {
        "activeClients": len(paid),
        "mrrUsd": float(sum(float(s["mrr_usd"]) for s in paid)),
        "growthCount": sum(1 for s in subs if s.get("tier") == "growth"),
        "starterCount": sum(1 for s in subs if s.get("tier") == "starter"),
        "overdueCount": sum(1 for s in subs if s.get("payment_status") == "overdue"),
    }


handler = lambda_handler(app)
