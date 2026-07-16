"""merchants service — B2B onboarding & subscription (Función 02).

Onboarding creates the tenant (merchant + subscription with tier-defaulted limits) and
records payment through the PaymentProvider stub (D9). The owner's login is provisioned
by the Supabase Auth invite in Phase 2; ownerInviteSent reflects that.
"""
from __future__ import annotations

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import NotFound, NotImplementedYet, TierLimit
from conpass_common.models import (
    MerchantOnboardRequest,
    OperationUserCreate,
)
from conpass_common.providers import get_payment_provider
from conpass_common.providers.payment import PaymentIntent

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

    return {
        "merchant": _merchant_model(merchant, sub),
        "subscription": _subscription_model(sub),
        # Supabase Auth invite is wired in Phase 2:
        "ownerInviteSent": False,
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
    # Creating the login requires a Supabase Auth invite (Phase 2).
    raise NotImplementedYet("Phase 2 — Supabase Auth invite for the operation user")


handler = lambda_handler(app)
