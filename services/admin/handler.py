"""admin service — platform-admin, cross-tenant (Función 04). Requires platform_admin."""
from datetime import UTC, datetime

from conpass_common import CurrentIdentity, create_app, lambda_handler, wallet_sync
from conpass_common.assets import presign_payment_proof_download, presign_payment_qr_upload
from conpass_common.config import settings
from conpass_common.email_templates import credentials_email
from conpass_common.errors import (
    Conflict,
    EmailDeliveryFailed,
    NotConfigured,
    NotFound,
    ValidationFailed,
)
from conpass_common.logging import get_logger
from conpass_common.models import (
    PaymentQrUploadRequest,
    PaymentSettingsUpdate,
    PlatformNotificationRulesUpdate,
    SubscriptionUpdate,
)
from conpass_common.notify.settings import load_platform_rules, platform_rules_model
from conpass_common.payment_settings import payment_settings_model
from conpass_common.plans import TIER_ORDER, estimate, get_plan, period_start, subscription_to_api
from conpass_common.providers import get_email_sender
from conpass_common.providers.email import EmailDeliveryError
from conpass_common.provisioning import get_user_language, issue_temp_password

from .repository import AdminRepository

log = get_logger(__name__)
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


def _notifications_by_merchant(repo: AdminRepository) -> dict[str, int]:
    return repo.notifications_by_merchant(period_start(datetime.now(UTC).date()))


def _admin_client_model(m: dict, sub: dict | None, active_cards: int = 0,
                        notifications: int = 0) -> dict:
    out = {
        "merchantId": m["id"], "name": m["business_name"], "city": m.get("city"),
        "tier": sub["tier"] if sub else None,
        "paymentStatus": sub["payment_status"] if sub else None,
        "since": m["created_at"],
    }
    if sub:
        plan = get_plan(sub["tier"])
        est = estimate(plan, active_cards, notifications)
        out["priceUsd"] = float(plan.price_usd)
        out["activeCards"] = active_cards
        out["includedCards"] = plan.included_cards
        out["estimatedMonthlyUsd"] = float(est.total_usd)
    else:
        out.update(priceUsd=0.0, activeCards=active_cards, includedCards=0,
                   estimatedMonthlyUsd=0.0)
    if sub and sub.get("next_charge_at") is not None:
        out["nextChargeAt"] = sub["next_charge_at"]
    if sub and sub.get("last_payment_at") is not None:
        out["lastPaymentAt"] = sub["last_payment_at"]
    out["hasPaymentProof"] = bool(sub and sub.get("payment_proof_key"))
    if sub and sub.get("payment_proof_uploaded_at") is not None:
        out["paymentProofUploadedAt"] = sub["payment_proof_uploaded_at"]
    return out


@app.get("/admin/clients")
def list_clients(identity: CurrentIdentity):
    identity.require_role("platform_admin")
    repo = get_repo()
    merchants = repo.list_merchants()
    subs_by_merchant = {s["merchant_id"]: s for s in repo.list_subscriptions()}
    active = repo.active_card_counts()   # one query for every merchant (no N+1)
    notifs = _notifications_by_merchant(repo)
    return [_admin_client_model(m, subs_by_merchant.get(m["id"]), active.get(m["id"], 0),
                                notifs.get(m["id"], 0))
            for m in merchants]


@app.patch("/admin/clients/{merchant_id}/subscription")
def update_subscription(merchant_id: str, body: SubscriptionUpdate, identity: CurrentIdentity):
    identity.require_role("platform_admin")
    repo = get_repo()
    existing = repo.get_subscription(merchant_id)
    if existing is None:
        raise NotFound("subscription not found")

    previous_tier = existing.get("tier")
    patch: dict = {}
    if body.tier is not None:
        patch["tier"] = body.tier.value
    if body.paymentStatus is not None:
        patch["payment_status"] = body.paymentStatus.value
    updated = repo.update_subscription(merchant_id, patch) if patch else existing
    if "tier" in patch and patch["tier"] != previous_tier:
        # Nearby locations on the wallet classes depend on the plan (Growth+).
        try:
            wallet_sync.sync_merchant_classes(repo.client, merchant_id)
        except Exception as exc:  # noqa: BLE001 - the plan change itself already succeeded
            log.warning("sync_merchant_classes_failed", extra={
                "merchant_id": merchant_id, "error": type(exc).__name__})

    active_pass_count = repo.active_pass_count(merchant_id)
    return subscription_to_api(updated, active_pass_count)


@app.post("/admin/clients/{merchant_id}/resend-credentials")
def resend_credentials(merchant_id: str, identity: CurrentIdentity):
    """Issue a fresh temporary password for the merchant owner and email it. The order
    of checks matters: nothing touches the password until the sender is known to work."""
    identity.require_role("platform_admin")
    repo = get_repo()
    merchant = repo.get_merchant(merchant_id)
    if merchant is None:
        raise NotFound("merchant not found")
    if merchant.get("is_demo"):
        raise Conflict("the demo account uses a shared password")
    owner = repo.owner_profile(merchant_id)
    if owner is None:
        raise NotFound("this merchant has no owner account")
    sender = get_email_sender()
    if not sender.configured:
        raise NotConfigured("email is not configured: set SMTP_USERNAME and SMTP_PASSWORD")

    lang = get_user_language(owner["user_id"])
    password = issue_temp_password(owner["user_id"])
    try:
        sender.send(owner["email"], *credentials_email(
            lang, owner_name=owner.get("name") or merchant.get("contact_name"),
            business_name=merchant["business_name"], email=owner["email"],
            temp_password=password, login_url=settings.console_login_url, resend=True))
    except EmailDeliveryError as exc:
        raise EmailDeliveryFailed(
            "the password was reset but the email could not be sent; retry") from exc
    return {"sent": True, "email": owner["email"]}


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
    repo = get_repo()
    subs = repo.list_subscriptions()
    active = repo.active_card_counts()
    paid = [s for s in subs if s.get("payment_status") == "paid"]
    subscription_mrr = sum(get_plan(s["tier"]).price_usd for s in paid)
    notifs = _notifications_by_merchant(repo)
    mrr = sum(estimate(get_plan(s["tier"]), active.get(s["merchant_id"], 0),
                       notifs.get(s["merchant_id"], 0)).total_usd
              for s in paid)
    return {
        "activeClients": len(paid),
        "mrrUsd": float(mrr),
        "subscriptionMrrUsd": float(subscription_mrr),
        "tierCounts": {t: sum(1 for s in subs if s.get("tier") == t) for t in TIER_ORDER},
        "overdueCount": sum(1 for s in subs if s.get("payment_status") == "overdue"),
    }


@app.get("/admin/notification-rules")
def get_notification_rules(identity: CurrentIdentity):
    identity.require_role("platform_admin")
    return platform_rules_model(load_platform_rules(get_repo().client))


@app.patch("/admin/notification-rules")
def update_notification_rules(body: PlatformNotificationRulesUpdate, identity: CurrentIdentity):
    """Partial: only the fields sent change (the singleton row, id = true)."""
    identity.require_role("platform_admin")
    repo = get_repo()
    sent = body.model_dump(exclude_unset=True, exclude_none=True)
    patch = {}
    if "capPerDay" in sent:
        patch["cap_per_day"] = sent["capPerDay"]
    if "quietHoursEnabled" in sent:
        patch["quiet_hours_enabled"] = sent["quietHoursEnabled"]
    if patch:
        patch["updated_at"] = datetime.now(UTC).isoformat()
        repo.update_notification_rules(patch)
    return platform_rules_model(load_platform_rules(repo.client))


handler = lambda_handler(app)
