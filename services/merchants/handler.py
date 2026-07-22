"""merchants service — B2B onboarding & subscription (Función 02).

Onboarding creates the tenant (merchant + subscription with tier-defaulted limits),
records payment through the PaymentProvider stub (D9), and provisions the owner's
Supabase Auth login (merchant_owner role in app_metadata) — returning a temp password
for first login (Función 03). Operators are provisioned the same way, tier-limited.
"""
from __future__ import annotations

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.demo import DEMO_PASSWORD
from conpass_common.errors import AppError, NotFound, TierLimit
from conpass_common.models import (
    MerchantOnboardRequest,
    OperationUserCreate,
    OperationUserUpdate,
)
from conpass_common.providers import get_payment_provider
from conpass_common.providers.payment import PaymentIntent
from conpass_common.provisioning import (
    delete_user,
    provision_user,
    reset_password,
    update_user,
)

from .repository import MerchantsRepository

app = create_app(service="merchants")


def get_repo() -> MerchantsRepository:
    return MerchantsRepository()


def _subscription_model(sub: dict) -> dict:
    return {
        "tier": sub["tier"],
        "paymentStatus": sub["payment_status"],
        "mrrUsd": float(sub["mrr_usd"]),
        "activePassLimit": sub.get("active_pass_limit"),
        "programLimit": sub.get("program_limit"),
        "operationUserLimit": sub.get("operation_user_limit"),
        "nextChargeAt": sub.get("next_charge_at"),
        "lastPaymentAt": sub.get("last_payment_at"),
    }


def _merchant_model(m: dict, sub: dict | None = None) -> dict:
    out = {
        "id": m["id"], "businessName": m["business_name"], "ruc": m.get("ruc"),
        "category": m.get("category"), "city": m.get("city"),
        "contactName": m.get("contact_name"), "contactEmail": m.get("contact_email"),
        "logoStorageKey": m.get("logo_storage_key"), "createdAt": m["created_at"],
    }
    if sub:
        out["subscription"] = _subscription_model(sub)
    return out


@app.post("/merchants", status_code=201)
def onboard_merchant(body: MerchantOnboardRequest):
    repo = get_repo()
    merchant = repo.create_merchant({
        "business_name": body.businessName,
        "ruc": body.ruc,
        "category": body.category.value,
        "city": body.city,
        "contact_name": body.contactName,
        "contact_email": body.contactEmail,
    })
    sub = repo.create_subscription(merchant["id"], body.tier.value)

    # Record payment (stub: manual proof; platform-admin confirms activation).
    get_payment_provider().submit(PaymentIntent(
        merchant_id=merchant["id"], tier=body.tier.value,
        amount_usd=float(sub["mrr_usd"]), method=body.payment.method.value,
        proof_storage_key=body.payment.proofStorageKey,
    ))

    # Provision the owner's login (Supabase Auth user with merchant_owner role).
    # Roll back the tenant if provisioning fails (e.g. email already in use).
    try:
        owner = provision_user(
            email=str(body.contactEmail), roles=["merchant_owner"],
            merchant_id=merchant["id"], name=body.contactName)
    except AppError:
        repo.delete_merchant(merchant["id"])
        raise

    return {
        "merchant": _merchant_model(merchant, sub),
        "subscription": _subscription_model(sub),
        "ownerInviteSent": True,
        "ownerEmail": owner.email,
        "ownerTempPassword": owner.temp_password,
    }


@app.get("/demo")
def get_demo_context():
    """Public: the most-recent demo tenant + shared test credentials, so the /demo
    sandbox can silently sign in. Demo logins only ever see demo-flagged data."""
    repo = get_repo()
    merchant = repo.latest_demo_merchant()
    if merchant is None:
        raise NotFound("no demo tenant is configured")
    program = repo.latest_program(merchant["id"])
    owner = repo.owner_profile(merchant["id"])
    if program is None or owner is None:
        raise NotFound("demo tenant is incomplete (missing program or owner)")
    return {
        "merchantId": merchant["id"],
        "businessName": merchant["business_name"],
        "programId": program["id"],
        "ownerEmail": owner["email"],
        "ownerPassword": DEMO_PASSWORD,
    }


@app.get("/merchants/{merchant_id}")
def get_merchant(merchant_id: str, identity: CurrentIdentity):
    identity.require_merchant(merchant_id)
    repo = get_repo()
    m = repo.get_merchant(merchant_id)
    if m is None:
        raise NotFound("merchant not found")
    return _merchant_model(m, repo.get_subscription(merchant_id))


@app.get("/merchants/{merchant_id}/operation-users")
def list_operation_users(merchant_id: str, identity: CurrentIdentity):
    identity.require_merchant(merchant_id)
    users = get_repo().list_operation_users(merchant_id)
    return [{"id": u["user_id"], "name": u.get("name"), "email": u.get("email"),
            "station": u.get("station")} for u in users]


@app.post("/merchants/{merchant_id}/operation-users", status_code=201)
def add_operation_user(merchant_id: str, body: OperationUserCreate, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    identity.require_merchant(merchant_id)
    repo = get_repo()
    sub = repo.get_subscription(merchant_id)
    limit = sub.get("operation_user_limit") if sub else None
    if limit is not None and repo.count_operation_users(merchant_id) >= limit:
        raise TierLimit(f"operation-user limit ({limit}) reached for this tier")

    op = provision_user(
        email=str(body.email), roles=["operation_user"],
        merchant_id=merchant_id, station=body.station.value, name=body.name)
    return {
        "id": op.user_id, "name": body.name, "email": op.email,
        "station": body.station.value, "tempPassword": op.temp_password,
    }


def _require_operation_user(
    merchant_id: str, operation_user_id: str, identity: CurrentIdentity,
) -> dict:
    identity.require_role("merchant_owner")
    identity.require_merchant(merchant_id)
    existing = get_repo().get_operation_user(merchant_id, operation_user_id)
    if existing is None:
        raise NotFound("operation user not found")
    return existing


@app.patch("/merchants/{merchant_id}/operation-users/{operation_user_id}")
def edit_operation_user(
    merchant_id: str, operation_user_id: str,
    body: OperationUserUpdate, identity: CurrentIdentity,
):
    existing = _require_operation_user(merchant_id, operation_user_id, identity)
    update_user(
        operation_user_id,
        email=str(body.email) if body.email is not None else None,
        station=body.station.value if body.station is not None else None,
        name=body.name,
    )
    return {
        "id": operation_user_id,
        "name": body.name if body.name is not None else existing.get("name"),
        "email": str(body.email) if body.email is not None else existing.get("email"),
        "station": body.station.value if body.station is not None else existing.get("station"),
    }


@app.delete("/merchants/{merchant_id}/operation-users/{operation_user_id}", status_code=204)
def remove_operation_user(merchant_id: str, operation_user_id: str, identity: CurrentIdentity):
    _require_operation_user(merchant_id, operation_user_id, identity)
    delete_user(operation_user_id)
    get_repo().delete_operation_user_profile(merchant_id, operation_user_id)
    return None


@app.post("/merchants/{merchant_id}/operation-users/{operation_user_id}/reset-password")
def reset_operation_user_password(
    merchant_id: str, operation_user_id: str, identity: CurrentIdentity,
):
    _require_operation_user(merchant_id, operation_user_id, identity)
    temp_password = reset_password(operation_user_id)
    return {"id": operation_user_id, "tempPassword": temp_password}


handler = lambda_handler(app)
