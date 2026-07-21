"""admin service — platform-admin, cross-tenant (Función 04). Requires platform_admin."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import NotFound
from conpass_common.models import SubscriptionUpdate

from .repository import AdminRepository

app = create_app(service="admin")


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
