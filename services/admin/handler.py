"""admin service — platform-admin, cross-tenant (Función 04). Requires platform_admin.

Phase 18 (D21): the merchants file (board 18) with verification + activation, payments ledger,
payment reminders, plan-change review and the platform settings (pricing phase + payment
details). Activation is what e-mails the owner's access details (not signup).
"""
from datetime import UTC, date, datetime
from decimal import Decimal

from conpass_common import CurrentIdentity, billing, create_app, lambda_handler, wallet_sync
from conpass_common.assets import presign_payment_proof_download, presign_payment_qr_upload
from conpass_common.config import settings
from conpass_common.email_templates import (
    activation_email,
    credentials_email,
    payment_reminder_email,
    plan_change_applied_email,
    plan_change_rejected_email,
    signup_rejected_email,
)
from conpass_common.errors import (
    AppError,
    Conflict,
    EmailDeliveryFailed,
    NotConfigured,
    NotFound,
    ValidationFailed,
)
from conpass_common.logging import get_logger
from conpass_common.models import (
    ClientActivation,
    ClientRejection,
    PaymentQrUploadRequest,
    PaymentRecord,
    PaymentSettingsUpdate,
    PlatformNotificationRulesUpdate,
    PlatformSettingsUpdate,
    SubscriptionUpdate,
)
from conpass_common.notify.settings import load_platform_rules, platform_rules_model
from conpass_common.payment_settings import payment_settings_model
from conpass_common.plans import (
    ANNUAL,
    LAUNCH,
    MONTHLY,
    TIER_ORDER,
    current_pricing_phase,
    get_plan,
    money,
    period_start,
    subscription_price_usd,
    subscription_to_api,
)
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


def _admin(identity: CurrentIdentity) -> dict:
    """Require platform_admin; the actor kwargs for `billing.log_activity`."""
    identity.require_role("platform_admin")
    return {"actor_user_id": identity.user_id, "actor_role": "platform_admin"}


def _notifications_by_merchant(repo: AdminRepository) -> dict[str, int]:
    return repo.notifications_by_merchant(period_start(billing.today_utc()))


def _has_signup_receipt(sub: dict | None, signup_pay: dict | None) -> bool:
    """A receipt exists: on the signup payment, or (older rows) on the legacy column."""
    return bool((signup_pay and signup_pay.get("proof_storage_key"))
                or (sub and sub.get("payment_proof_key")))


def _is_pending_verification(sub: dict | None, signup_pay: dict | None) -> bool:
    return bool(sub and sub.get("payment_status") == "pending"
                and _has_signup_receipt(sub, signup_pay))


def _admin_client_model(m: dict, sub: dict | None, *, active_cards: int = 0,
                        notifications: int = 0, locations: int = 0,
                        signup_pay: dict | None = None, review_pending: bool = False,
                        phase: str = LAUNCH, today: date | None = None) -> dict:
    out = {
        "merchantId": m["id"], "name": m["business_name"], "city": m.get("city"),
        "tier": sub["tier"] if sub else None,
        "paymentStatus": sub["payment_status"] if sub else None,
        "since": m["created_at"],
    }
    if sub:
        plan = get_plan(sub["tier"])
        est = billing.month_estimate(sub, active_cards, notifications, phase, today)
        out["priceUsd"] = float(subscription_price_usd(sub, phase))
        out["activeCards"] = active_cards
        out["includedCards"] = plan.included_cards
        out["estimatedMonthlyUsd"] = float(est.total_usd)
        out["billingPeriod"] = sub.get("billing_period") or MONTHLY
    else:
        out.update(priceUsd=0.0, activeCards=active_cards, includedCards=0,
                   estimatedMonthlyUsd=0.0)
    if sub and sub.get("next_charge_at") is not None:
        out["nextChargeAt"] = sub["next_charge_at"]
    if sub and sub.get("last_payment_at") is not None:
        out["lastPaymentAt"] = sub["last_payment_at"]
    has_proof = _has_signup_receipt(sub, signup_pay)
    out["hasPaymentProof"] = has_proof
    uploaded_at = (sub or {}).get("payment_proof_uploaded_at") or (
        signup_pay["created_at"] if has_proof and signup_pay else None)
    if uploaded_at is not None:
        out["paymentProofUploadedAt"] = uploaded_at
    for api, col in (("ruc", "ruc"), ("contactName", "contact_name"),
                     ("contactEmail", "contact_email"), ("contactPhone", "contact_phone")):
        if m.get(col):
            out[api] = m[col]
    out["notificationsUsed"] = notifications
    out["locationCount"] = locations
    out["pendingVerification"] = _is_pending_verification(sub, signup_pay)
    out["hasPendingPlanChange"] = review_pending
    return out


@app.get("/admin/clients")
def list_clients(identity: CurrentIdentity):
    _admin(identity)
    repo = get_repo()
    merchants = repo.list_merchants()
    subs_by_merchant = {s["merchant_id"]: s for s in repo.list_subscriptions()}
    # One query per source for every merchant (no N+1).
    active = repo.active_card_counts()
    notifs = _notifications_by_merchant(repo)
    locations = repo.active_location_counts()
    signups = repo.signup_payments()
    reviewing = repo.merchants_with_review_pending()
    phase = current_pricing_phase(repo.client)
    today = billing.today_utc()
    return [_admin_client_model(
        m, subs_by_merchant.get(m["id"]), active_cards=active.get(m["id"], 0),
        notifications=notifs.get(m["id"], 0), locations=locations.get(m["id"], 0),
        signup_pay=signups.get(m["id"]), review_pending=m["id"] in reviewing,
        phase=phase, today=today)
        for m in merchants]


# --------------------------------------------------------------------------- #
# The merchant file (board 18)
# --------------------------------------------------------------------------- #
def _sync_classes(repo: AdminRepository, merchant_id: str) -> None:
    """Nearby locations on the wallet classes follow the plan; best-effort."""
    try:
        wallet_sync.sync_merchant_classes(repo.client, merchant_id)
    except Exception as exc:  # noqa: BLE001 - the plan change itself already succeeded
        log.warning("sync_merchant_classes_failed", extra={
            "merchant_id": merchant_id, "error": type(exc).__name__})


def _merchant_and_sub(repo: AdminRepository, merchant_id: str) -> tuple[dict, dict]:
    merchant = repo.get_merchant(merchant_id)
    if merchant is None:
        raise NotFound("merchant not found")
    sub = repo.get_subscription(merchant_id)
    if sub is None:
        raise NotFound("subscription not found")
    return merchant, sub


def _client_detail(repo: AdminRepository, merchant_id: str) -> dict:
    merchant, sub = _merchant_and_sub(repo, merchant_id)
    client = repo.client
    phase = current_pricing_phase(client)
    usage = billing.usage_for(client, merchant_id)
    signup_pay = billing.signup_payment(client, merchant_id)
    open_req = billing.open_plan_change(client, merchant_id)
    pending_api = billing.plan_changes_to_api(client, [open_req])[0] if open_req else None

    sub_api = subscription_to_api(sub, usage["activeCards"], phase)
    if pending_api:
        sub_api["pendingChange"] = pending_api
    owner = repo.owner_profile(merchant_id)
    out = {
        "client": _admin_client_model(
            merchant, sub, active_cards=usage["activeCards"],
            notifications=usage["notificationsUsed"], locations=usage["locationCount"],
            signup_pay=signup_pay,
            review_pending=bool(open_req and open_req["status"] == "pending_review"),
            phase=phase),
        "merchant": billing.merchant_to_api(merchant, sub_api),
        "subscription": sub_api,
        "usage": usage,
        "payments": [billing.payment_to_api(p) for p in billing.list_payments(client, merchant_id)],
        "activity": billing.list_activity(client, merchant_id, 50),
    }
    if owner and owner.get("email"):
        out["ownerEmail"] = owner["email"]
    if pending_api:
        out["planChange"] = pending_api
    return out


@app.get("/admin/clients/{merchant_id}")
def get_client_detail(merchant_id: str, identity: CurrentIdentity):
    _admin(identity)
    return _client_detail(get_repo(), merchant_id)


def _owner_language(owner: dict | None) -> str:
    try:
        return get_user_language(owner["user_id"]) if owner else "es"
    except Exception:  # noqa: BLE001 - copy language is cosmetic; default to Spanish
        return "es"


def _owner_recipient(merchant: dict, owner: dict | None) -> tuple[str | None, str | None]:
    """(address, name) the merchant's e-mails go to: the owner's login, else the signup contact."""
    email = (owner or {}).get("email") or merchant.get("contact_email")
    name = (owner or {}).get("name") or merchant.get("contact_name")
    return email, name


def _require_not_demo(merchant: dict) -> None:
    if merchant.get("is_demo"):
        raise Conflict("the demo account uses a shared password")


@app.post("/admin/clients/{merchant_id}/activate")
def activate_client(merchant_id: str, body: ClientActivation, identity: CurrentIdentity):
    """Verify (RUC + receipt), activate and e-mail the access details. The activation is
    committed BEFORE the e-mail, so a delivery failure answers 502 but the account stays
    active; admin can resend the access details."""
    actor = _admin(identity)
    if not (body.rucVerified and body.proofVerified):
        raise ValidationFailed("verify the RUC and the receipt before activating",
                               details={"reason": "verification_incomplete"})
    repo = get_repo()
    client = repo.client
    merchant, sub = _merchant_and_sub(repo, merchant_id)
    _require_not_demo(merchant)
    if sub["payment_status"] == "paid":
        raise Conflict("this account is already active", details={"reason": "already_active"})
    owner = repo.owner_profile(merchant_id)
    if owner is None:
        raise NotFound("this merchant has no owner account")

    today = billing.today_utc()
    phase = current_pricing_phase(client)
    period = sub.get("billing_period") or MONTHLY
    next_charge = billing.add_period(today, period)

    pay = billing.signup_payment(client, merchant_id)
    if pay is None:    # older signup with no ledger row: record what was due
        pay = billing.create_payment(client, {
            "merchant_id": merchant_id, "kind": "signup", "tier": sub["tier"],
            "billing_period": period, "status": "pending", "method": "manual_transfer",
            "amount_usd": float(billing.signup_amount(sub["tier"], period, phase)),
            "proof_storage_key": sub.get("payment_proof_key")})
    billing.confirm_payment(client, pay["id"], confirmed_by=identity.user_id, paid_at=today,
                            period_start_=today, period_end=next_charge)

    patch = {"payment_status": "paid", "last_payment_at": today.isoformat(),
             "next_charge_at": next_charge.isoformat(), "rejection_reason": None}
    if period == ANNUAL:
        patch["locked_price_usd"] = float(get_plan(sub["tier"]).annual_usd(phase))
    repo.update_subscription(merchant_id, patch)
    billing.log_activity(client, merchant_id, "activated", **actor, details={
        "tier": sub["tier"], "billingPeriod": period, "nextChargeAt": next_charge.isoformat()})

    # Committed. From here a failure is a 502 that leaves the activation in place.
    sender = get_email_sender()
    if not sender.configured:
        raise EmailDeliveryFailed(
            "the account is active but email is not configured; "
            "configure it and resend the access details")
    to, name = _owner_recipient(merchant, owner)
    try:
        password = issue_temp_password(owner["user_id"])
    except AppError as exc:
        raise EmailDeliveryFailed(
            "the account is active but the access details could not be issued; "
            "resend the access details") from exc
    try:
        sender.send(to, *activation_email(
            _owner_language(owner), owner_name=name, business_name=merchant["business_name"],
            email=owner["email"], temp_password=password, login_url=settings.console_login_url))
    except EmailDeliveryError as exc:
        raise EmailDeliveryFailed(
            "the account is active but the email could not be sent; "
            "resend the access details") from exc
    return _client_detail(repo, merchant_id)


@app.post("/admin/clients/{merchant_id}/reject")
def reject_client(merchant_id: str, body: ClientRejection, identity: CurrentIdentity):
    """Reject a signup with a reason, which is e-mailed. The state is saved before the e-mail,
    so a delivery failure answers 502 but the rejection stands."""
    actor = _admin(identity)
    repo = get_repo()
    client = repo.client
    merchant, sub = _merchant_and_sub(repo, merchant_id)
    if sub["payment_status"] == "paid":
        raise Conflict("this account is already active; suspend it instead",
                       details={"reason": "already_active"})
    reason = body.reason.strip()
    if not reason:
        raise ValidationFailed("a reason is required", details={"reason": "reason_required"})

    repo.update_subscription(merchant_id, {"payment_status": "rejected",
                                           "rejection_reason": reason})
    pay = billing.signup_payment(client, merchant_id)
    if pay is not None:
        billing.reject_payment(client, pay["id"], reason)
    billing.log_activity(client, merchant_id, "rejected", **actor, details={"reason": reason})

    owner = repo.owner_profile(merchant_id)
    to, name = _owner_recipient(merchant, owner)
    try:
        if not to:
            raise EmailDeliveryError("no recipient")
        get_email_sender().send(to, *signup_rejected_email(
            _owner_language(owner), owner_name=name, business_name=merchant["business_name"],
            reason=reason))
    except EmailDeliveryError as exc:
        raise EmailDeliveryFailed(
            "the signup was rejected but the email could not be sent") from exc
    return _client_detail(repo, merchant_id)


@app.post("/admin/clients/{merchant_id}/payments", status_code=201)
def record_client_payment(merchant_id: str, body: PaymentRecord, identity: CurrentIdentity):
    """Register a payment that arrived (a confirmed ledger row). A renewal moves the next
    charge one period ahead; an annual renewal refreshes the locked price at today's phase."""
    actor = _admin(identity)
    repo = get_repo()
    client = repo.client
    _, sub = _merchant_and_sub(repo, merchant_id)
    period = sub.get("billing_period") or MONTHLY
    paid_at = body.paidAt
    kind = body.kind.value

    row = {
        "merchant_id": merchant_id, "kind": kind, "tier": sub["tier"], "billing_period": period,
        "amount_usd": float(money(Decimal(str(body.amountUsd)))), "method": body.method.value,
        "status": "confirmed", "paid_at": paid_at.isoformat(), "confirmed_by": identity.user_id,
        "confirmed_at": billing.now_iso(),
    }
    if body.note:
        row["note"] = body.note
    if body.periodStart:
        row["period_start"] = body.periodStart.isoformat()
    sub_patch = {"payment_status": "paid", "last_payment_at": paid_at.isoformat()}
    if kind == "renewal":
        base = billing.as_date(sub.get("next_charge_at")) or paid_at
        new_next = billing.add_period(base, period)
        sub_patch["next_charge_at"] = new_next.isoformat()
        row.setdefault("period_start", base.isoformat())
        row["period_end"] = new_next.isoformat()
        if period == ANNUAL:
            sub_patch["locked_price_usd"] = float(
                get_plan(sub["tier"]).annual_usd(current_pricing_phase(client)))
    payment = billing.create_payment(client, row)
    repo.update_subscription(merchant_id, sub_patch)
    billing.log_activity(client, merchant_id, "payment_recorded", **actor, details={
        "kind": kind, "amountUsd": row["amount_usd"], "method": body.method.value,
        "paidAt": paid_at.isoformat()})
    return billing.payment_to_api(payment)


@app.get("/admin/clients/{merchant_id}/payments/{payment_id}/receipt")
def get_client_payment_receipt(merchant_id: str, payment_id: str, identity: CurrentIdentity):
    _admin(identity)
    return billing.payment_receipt_link(get_repo().client, merchant_id, payment_id)


@app.post("/admin/clients/{merchant_id}/payment-reminder")
def send_payment_reminder(merchant_id: str, identity: CurrentIdentity):
    """E-mail the amount due (this month's estimate, IVA included) plus the payment details."""
    actor = _admin(identity)
    repo = get_repo()
    client = repo.client
    merchant, _ = _merchant_and_sub(repo, merchant_id)
    _require_not_demo(merchant)
    owner = repo.owner_profile(merchant_id)
    to, name = _owner_recipient(merchant, owner)
    if not to:
        raise NotFound("this merchant has no e-mail address")
    sender = get_email_sender()
    if not sender.configured:
        raise NotConfigured("email is not configured: set SMTP_USERNAME and SMTP_PASSWORD")

    amount = billing.usage_for(client, merchant_id)["estimatedTotalUsd"]
    try:
        sender.send(to, *payment_reminder_email(
            _owner_language(owner), owner_name=name, business_name=merchant["business_name"],
            amount_usd=amount, payment_settings=repo.get_payment_settings()))
    except EmailDeliveryError as exc:
        raise EmailDeliveryFailed("the reminder could not be sent; retry") from exc
    billing.log_activity(client, merchant_id, "reminder_sent", **actor,
                         details={"amountUsd": amount})
    return {"sent": True, "email": to}


@app.patch("/admin/clients/{merchant_id}/subscription")
def update_subscription(merchant_id: str, body: SubscriptionUpdate, identity: CurrentIdentity):
    actor = _admin(identity)
    repo = get_repo()
    existing = repo.get_subscription(merchant_id)
    if existing is None:
        raise NotFound("subscription not found")

    previous_tier = existing.get("tier")
    previous_status = existing.get("payment_status")
    patch: dict = {}
    if body.tier is not None:
        patch["tier"] = body.tier.value
    if body.paymentStatus is not None:
        patch["payment_status"] = body.paymentStatus.value
    updated = repo.update_subscription(merchant_id, patch) if patch else existing
    if "tier" in patch and patch["tier"] != previous_tier:
        _sync_classes(repo, merchant_id)    # nearby locations follow the plan
    new_status = patch.get("payment_status")
    if new_status and new_status != previous_status:
        if new_status == "suspended":
            action = "suspended"
        elif previous_status == "suspended":
            action = "reactivated"
        else:
            action = None
        if action:
            billing.log_activity(repo.client, merchant_id, action, **actor,
                                 details={"from": previous_status, "to": new_status})

    return billing.subscription_for_api(
        repo.client, updated, active_cards=repo.active_pass_count(merchant_id))


@app.post("/admin/clients/{merchant_id}/resend-credentials")
def resend_credentials(merchant_id: str, identity: CurrentIdentity):
    """Issue a fresh temporary password for the merchant owner and email it. The order
    of checks matters: nothing touches the password until the sender is known to work."""
    actor = _admin(identity)
    repo = get_repo()
    merchant = repo.get_merchant(merchant_id)
    if merchant is None:
        raise NotFound("merchant not found")
    _require_not_demo(merchant)
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
    billing.log_activity(repo.client, merchant_id, "credentials_resent", **actor)
    return {"sent": True, "email": owner["email"]}


@app.get("/admin/clients/{merchant_id}/payment-proof")
def get_client_payment_proof(merchant_id: str, identity: CurrentIdentity):
    """The signup receipt: the latest signup payment's, else the legacy subscription column."""
    _admin(identity)
    repo = get_repo()
    pay = billing.signup_payment(repo.client, merchant_id)
    sub = repo.get_subscription(merchant_id)
    if pay and pay.get("proof_storage_key"):
        proof_key, uploaded_at = pay["proof_storage_key"], pay["created_at"]
    else:
        proof_key = sub.get("payment_proof_key") if sub else None
        uploaded_at = sub.get("payment_proof_uploaded_at") if sub else None
    if not proof_key:
        raise NotFound("this client has not uploaded a payment proof")
    return {"url": presign_payment_proof_download(proof_key), "uploadedAt": uploaded_at}


# --------------------------------------------------------------------------- #
# Plan changes (O4)
# --------------------------------------------------------------------------- #
def _reviewable_request(client, request_id: str) -> dict:
    req = billing.get_plan_change(client, request_id)
    if req is None:
        raise NotFound("plan change request not found")
    if req["status"] not in billing.REVIEWABLE_STATUSES:
        raise Conflict("this request is not waiting for review",
                       details={"reason": "plan_change_not_reviewable", "status": req["status"]})
    return req


def _notify_best_effort(repo: AdminRepository, merchant_id: str, build) -> None:
    """E-mail the merchant about a plan-change decision. The decision is already saved and the
    console shows it, so a delivery failure is logged, not returned."""
    try:
        merchant = repo.get_merchant(merchant_id)
        owner = repo.owner_profile(merchant_id)
        to, name = _owner_recipient(merchant or {}, owner)
        if merchant is None or not to:
            return
        get_email_sender().send(to, *build(_owner_language(owner), name, merchant))
    except Exception as exc:  # noqa: BLE001
        log.error("plan_change_email_failed", extra={
            "merchant_id": merchant_id, "error": type(exc).__name__})


@app.post("/admin/plan-changes/{request_id}/approve")
def approve_plan_change(request_id: str, identity: CurrentIdentity):
    actor = _admin(identity)
    repo = get_repo()
    client = repo.client
    req = _reviewable_request(client, request_id)
    merchant_id = req["merchant_id"]
    _, sub = _merchant_and_sub(repo, merchant_id)

    today = billing.today_utc()
    phase = current_pricing_phase(client)
    to_tier, to_period = req["to_tier"], req["to_period"]
    new_next = billing.add_period(today, to_period)    # the new period starts today
    if req.get("payment_id"):
        billing.confirm_payment(client, req["payment_id"], confirmed_by=identity.user_id,
                                paid_at=today, period_start_=today, period_end=new_next)
    repo.update_subscription(merchant_id, {
        **billing.term_patch(to_tier, to_period, phase),
        "next_charge_at": new_next.isoformat(), "last_payment_at": today.isoformat()})
    updated = billing.update_plan_change(client, request_id, {
        "status": "applied", "resolved_by": identity.user_id, "resolved_at": billing.now_iso()})
    if to_tier != sub["tier"]:
        _sync_classes(repo, merchant_id)
    billing.log_activity(client, merchant_id, "plan_changed", **actor, details={
        "requestId": request_id,
        "from": {"tier": req["from_tier"], "billingPeriod": req["from_period"]},
        "to": {"tier": to_tier, "billingPeriod": to_period}})
    _notify_best_effort(repo, merchant_id, lambda lang, name, merchant: plan_change_applied_email(
        lang, owner_name=name, business_name=merchant["business_name"], tier=to_tier,
        period=to_period, next_charge_at=new_next.isoformat()))
    return billing.plan_changes_to_api(client, [updated or {**req, "status": "applied"}])[0]


@app.post("/admin/plan-changes/{request_id}/reject")
def reject_plan_change(request_id: str, body: ClientRejection, identity: CurrentIdentity):
    actor = _admin(identity)
    repo = get_repo()
    client = repo.client
    req = _reviewable_request(client, request_id)
    merchant_id = req["merchant_id"]
    reason = body.reason.strip()
    if not reason:
        raise ValidationFailed("a reason is required", details={"reason": "reason_required"})

    updated = billing.update_plan_change(client, request_id, {
        "status": "rejected", "rejection_reason": reason, "resolved_by": identity.user_id,
        "resolved_at": billing.now_iso()})
    if req.get("payment_id"):
        billing.reject_payment(client, req["payment_id"], reason)
    billing.log_activity(client, merchant_id, "plan_change_rejected", **actor, details={
        "requestId": request_id, "reason": reason})
    _notify_best_effort(repo, merchant_id, lambda lang, name, merchant: plan_change_rejected_email(
        lang, owner_name=name, business_name=merchant["business_name"], reason=reason))
    return billing.plan_changes_to_api(client, [updated or {**req, "status": "rejected"}])[0]


# --------------------------------------------------------------------------- #
# Platform settings
# --------------------------------------------------------------------------- #
def _platform_settings_model(repo: AdminRepository) -> dict:
    row = repo.get_platform_settings() or {}
    out = {"pricingPhase": row.get("pricing_phase") or LAUNCH,
           "paymentSettings": payment_settings_model(repo.get_payment_settings())}
    if row.get("list_price_effective_at"):
        out["listPriceEffectiveAt"] = row["list_price_effective_at"]
    return out


@app.get("/admin/platform-settings")
def get_platform_settings(identity: CurrentIdentity):
    _admin(identity)
    return _platform_settings_model(get_repo())


@app.patch("/admin/platform-settings")
def update_platform_settings(body: PlatformSettingsUpdate, identity: CurrentIdentity):
    """Partial: only the fields sent change; an explicit null clears the announced date."""
    _admin(identity)
    repo = get_repo()
    sent = body.model_dump(exclude_unset=True, mode="json")
    patch: dict = {}
    if sent.get("pricingPhase") is not None:
        patch["pricing_phase"] = sent["pricingPhase"]
    if "listPriceEffectiveAt" in sent:
        patch["list_price_effective_at"] = sent["listPriceEffectiveAt"]
    if patch:
        patch["updated_at"] = datetime.now(UTC).isoformat()
        repo.update_platform_settings(patch)
    return _platform_settings_model(repo)


@app.patch("/admin/payment-settings")
def update_payment_settings(body: PaymentSettingsUpdate, identity: CurrentIdentity):
    _admin(identity)
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
    _admin(identity)
    try:
        return presign_payment_qr_upload(content_type=body.contentType.value)
    except RuntimeError as exc:
        raise NotConfigured(str(exc)) from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@app.get("/admin/stats")
def get_platform_stats(identity: CurrentIdentity):
    _admin(identity)
    repo = get_repo()
    subs = repo.list_subscriptions()
    active = repo.active_card_counts()
    notifs = _notifications_by_merchant(repo)
    signups = repo.signup_payments()
    phase = current_pricing_phase(repo.client)
    today = billing.today_utc()
    paid = [s for s in subs if s.get("payment_status") == "paid"]

    # Monthly-equivalent subscription: an annual price counts a twelfth each month.
    subscription_mrr = sum(
        subscription_price_usd(s, phase) / (12 if s.get("billing_period") == ANNUAL else 1)
        for s in paid)
    mrr = sum(billing.month_estimate(
        s, active.get(s["merchant_id"], 0), notifs.get(s["merchant_id"], 0), phase, today
    ).total_usd for s in paid)
    return {
        "activeClients": len(paid),
        "mrrUsd": float(money(Decimal(mrr))),
        "subscriptionMrrUsd": float(money(Decimal(subscription_mrr))),
        "tierCounts": {t: sum(1 for s in subs if s.get("tier") == t) for t in TIER_ORDER},
        "overdueCount": sum(1 for s in subs if s.get("payment_status") == "overdue"),
        "pendingVerificationCount": sum(
            1 for s in subs if _is_pending_verification(s, signups.get(s["merchant_id"]))),
        "pendingPlanChangeCount": len(repo.merchants_with_review_pending()),
        "suspendedCount": sum(1 for s in subs if s.get("payment_status") == "suspended"),
    }


@app.get("/admin/notification-rules")
def get_notification_rules(identity: CurrentIdentity):
    _admin(identity)
    return platform_rules_model(load_platform_rules(get_repo().client))


@app.patch("/admin/notification-rules")
def update_notification_rules(body: PlatformNotificationRulesUpdate, identity: CurrentIdentity):
    """Partial: only the fields sent change (the singleton row, id = true)."""
    _admin(identity)
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
