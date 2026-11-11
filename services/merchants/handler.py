"""merchants service — B2B onboarding & subscription (Función 02).

Onboarding creates the tenant (merchant + subscription; price and limits come from the
plan catalog), records payment through the PaymentProvider stub (D9), and provisions the owner's
Supabase Auth login (merchant_owner role in app_metadata) with a temporary password that
is EMAILED to the owner (never returned in the response) and must be changed at first
login (Función 03). Operators are provisioned the same way, tier-limited; their temp
password is still shown in the panel.
"""
from __future__ import annotations

from datetime import UTC, datetime

from conpass_common import CurrentIdentity, create_app, lambda_handler, wallet_sync
from conpass_common.assets import presign_payment_proof_upload
from conpass_common.config import settings
from conpass_common.demo import DEMO_PASSWORD
from conpass_common.email_templates import credentials_email
from conpass_common.errors import (
    AppError,
    Conflict,
    NotConfigured,
    NotFound,
    TierLimit,
    ValidationFailed,
)
from conpass_common.logging import get_logger
from conpass_common.models import (
    LocationCreate,
    LocationUpdate,
    MerchantOnboardRequest,
    Method,
    OperationUserCreate,
    OperationUserUpdate,
    PaymentProofUploadRequest,
)
from conpass_common.notify.settings import MAX_ACTIVE_LOCATIONS
from conpass_common.payment_settings import payment_settings_model
from conpass_common.plans import (
    PLANS,
    TIER_ORDER,
    estimate,
    get_plan,
    next_tier_with_more_operation_users,
    period_start,
    plan_to_api,
    subscription_to_api,
)
from conpass_common.providers import get_email_sender, get_payment_provider
from conpass_common.providers.payment import PaymentIntent
from conpass_common.provisioning import (
    delete_user,
    provision_user,
    reset_password,
    update_user,
)

from .repository import MerchantsRepository

log = get_logger(__name__)

# Manual-transfer / DEUNA payments can't be confirmed without a receipt — platform-admin
# has nothing to check against otherwise.
PROOF_REQUIRED_METHODS = {Method.deuna, Method.manual_transfer}

app = create_app(service="merchants")


def get_repo() -> MerchantsRepository:
    return MerchantsRepository()


def _subscription_model(sub: dict, active_cards: int | None = None) -> dict:
    return subscription_to_api(sub, active_cards)


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


@app.get("/payment-settings")
def get_payment_settings():
    """Public: the transfer/DEUNA account details shown on the signup page (Función
    02) — platform-admin maintains them in Función 04 (PATCH /admin/payment-settings)."""
    return payment_settings_model(get_repo().get_payment_settings())


@app.get("/plans")
def list_plans():
    """Public: the plan catalog (prices, inclusions, features) for the signup page and
    the in-app plan screen. Single source: conpass_common.plans."""
    return [plan_to_api(PLANS[t]) for t in TIER_ORDER]


@app.post("/payment-proofs/upload-url")
def create_payment_proof_upload_url(body: PaymentProofUploadRequest):
    """Public: presigned PUT for a manual-transfer/DEUNA receipt, uploaded before the
    account exists — the returned storageKey is submitted back as
    payment.proofStorageKey on POST /merchants."""
    try:
        result = presign_payment_proof_upload(content_type=body.contentType.value)
    except RuntimeError as exc:
        raise NotConfigured(str(exc)) from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return {"uploadUrl": result["uploadUrl"], "storageKey": result["storageKey"]}


@app.post("/merchants", status_code=201)
def onboard_merchant(body: MerchantOnboardRequest):
    proof_key = body.payment.proofStorageKey
    if body.payment.method in PROOF_REQUIRED_METHODS and not proof_key:
        raise ValidationFailed(
            f"payment.proofStorageKey is required for payment.method="
            f"{body.payment.method.value} — upload the receipt via "
            f"POST /payment-proofs/upload-url first")

    repo = get_repo()
    merchant = repo.create_merchant({
        "business_name": body.businessName,
        "ruc": body.ruc,
        "category": body.category.value,
        "city": body.city,
        "contact_name": body.contactName,
        "contact_email": body.contactEmail,
    })
    sub = repo.create_subscription(
        merchant["id"], body.tier.value,
        payment_proof_key=proof_key,
        payment_proof_uploaded_at=datetime.now(UTC).isoformat() if proof_key else None,
    )

    # Record payment (stub: manual proof; platform-admin confirms activation).
    get_payment_provider().submit(PaymentIntent(
        merchant_id=merchant["id"], tier=body.tier.value,
        amount_usd=float(get_plan(body.tier.value).price_usd), method=body.payment.method.value,
        proof_storage_key=body.payment.proofStorageKey,
    ))

    # Provision the owner's login (Supabase Auth user with merchant_owner role).
    # Roll back the tenant if provisioning fails (e.g. email already in use).
    lang = body.language.value if body.language is not None else "es"
    try:
        owner = provision_user(
            email=str(body.contactEmail), roles=["merchant_owner"],
            merchant_id=merchant["id"], name=body.contactName,
            must_change_password=True, language=lang)
    except AppError:
        repo.delete_merchant(merchant["id"])
        raise

    # Email the temp password. A delivery failure must not undo the signup: the account
    # exists and platform-admin can resend credentials (POST /admin/clients/.../resend).
    invite_sent = True
    try:
        get_email_sender().send(owner.email, *credentials_email(
            lang, owner_name=body.contactName, business_name=body.businessName,
            email=owner.email, temp_password=owner.temp_password,
            login_url=settings.console_login_url))
    except Exception as exc:  # noqa: BLE001 — EmailDeliveryError or a misconfigured sender
        invite_sent = False
        log.error("owner_credentials_email_failed", extra={
            "merchant_id": merchant["id"], "error": type(exc).__name__})

    return {
        "merchant": _merchant_model(merchant, sub),
        "subscription": _subscription_model(sub),
        "ownerInviteSent": invite_sent,
        "ownerEmail": owner.email,
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
    ctx = {
        "merchantId": merchant["id"],
        "businessName": merchant["business_name"],
        "programId": program["id"],
        "ownerEmail": owner["email"],
        "ownerPassword": DEMO_PASSWORD,
    }
    # The seeded customer's card, so the demo cashier can be pre-filled. Read-only.
    code = repo.sample_card_code(merchant["id"], program["id"])
    if code:
        ctx["sampleCardCode"] = code
    return ctx


@app.get("/merchants/{merchant_id}")
def get_merchant(merchant_id: str, identity: CurrentIdentity):
    identity.require_merchant(merchant_id)
    repo = get_repo()
    m = repo.get_merchant(merchant_id)
    if m is None:
        raise NotFound("merchant not found")
    return _merchant_model(m, repo.get_subscription(merchant_id))


@app.get("/merchants/{merchant_id}/usage")
def get_usage(merchant_id: str, identity: CurrentIdentity):
    """Plan consumption for the current billing month + the estimated invoice."""
    identity.require_merchant(merchant_id)   # owner of THIS merchant, or platform_admin
    repo = get_repo()
    sub = repo.get_subscription(merchant_id)
    if sub is None:
        raise NotFound("subscription not found")
    plan = get_plan(sub["tier"])
    active_cards = repo.active_card_count(merchant_id)
    notifications_used = repo.notifications_used(
        merchant_id, period_start(datetime.now(UTC).date()))
    est = estimate(plan, active_cards, notifications_used)
    return {
        "plan": plan_to_api(plan),
        "activeCards": active_cards,
        "extraCards": est.extra_cards,
        "extraCardsUsd": float(est.extra_cards_usd),
        "notificationsUsed": notifications_used,
        "extraNotifications": est.extra_notifications,
        "extraNotificationsUsd": float(est.extra_notifications_usd),
        "estimatedTotalUsd": float(est.total_usd),
        "programCount": repo.count_programs(merchant_id),
        "operationUserCount": repo.count_operation_users(merchant_id),
        "periodStart": period_start(datetime.now(UTC).date()),
    }


def _location_ids_by_user(repo: MerchantsRepository, merchant_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in repo.location_assignments(merchant_id):
        out.setdefault(row["user_id"], []).append(row["location_id"])
    return out


def _validated_location_ids(
    repo: MerchantsRepository, merchant_id: str, ids: list | None,
) -> list[str] | None:
    """None (field omitted) stays None; otherwise every id must be one of THIS merchant's
    locales — assigning a foreign locale would leak another tenant's store into /me."""
    if ids is None:
        return None
    wanted = list(dict.fromkeys(str(i) for i in ids))
    owned = {r["id"] for r in repo.list_locations(merchant_id)}
    foreign = [i for i in wanted if i not in owned]
    if foreign:
        raise ValidationFailed(
            "locationIds must be locales of this merchant",
            details={"reason": "location_not_found", "locationIds": foreign})
    return wanted


def _sync_classes(repo: MerchantsRepository, merchant_id: str) -> None:
    """Locales/plan feed the wallet class's nearby points; refresh best-effort — the owner's
    edit already succeeded and must not fail on a wallet hiccup."""
    try:
        wallet_sync.sync_merchant_classes(repo.client, merchant_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("sync_merchant_classes_failed", extra={
            "merchant_id": merchant_id, "error": type(exc).__name__})


@app.get("/merchants/{merchant_id}/operation-users")
def list_operation_users(merchant_id: str, identity: CurrentIdentity):
    identity.require_merchant(merchant_id)
    repo = get_repo()
    users = repo.list_operation_users(merchant_id)
    assigned = _location_ids_by_user(repo, merchant_id)
    return [{"id": u["user_id"], "name": u.get("name"), "email": u.get("email"),
            "station": u.get("station"), "locationIds": assigned.get(u["user_id"], [])}
            for u in users]


@app.post("/merchants/{merchant_id}/operation-users", status_code=201)
def add_operation_user(merchant_id: str, body: OperationUserCreate, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    identity.require_merchant(merchant_id)
    repo = get_repo()
    sub = repo.get_subscription(merchant_id)
    if sub is not None:
        tier = sub["tier"]
        limit = get_plan(tier).operation_user_limit   # None = unlimited
        if limit is not None and repo.count_operation_users(merchant_id) >= limit:
            details = {"limit": limit, "tier": tier}
            required = next_tier_with_more_operation_users(tier)
            if required is not None:
                details["requiredTier"] = required
            raise TierLimit(
                f"operation-user limit ({limit}) reached for the {tier} plan",
                details=details)

    # Validate before provisioning so a bad locale never leaves an orphan login behind.
    location_ids = _validated_location_ids(repo, merchant_id, body.locationIds) or []
    op = provision_user(
        email=str(body.email), roles=["operation_user"],
        merchant_id=merchant_id, station=body.station.value, name=body.name)
    if location_ids:
        repo.set_operation_user_locations(op.user_id, location_ids)
    return {
        "id": op.user_id, "name": body.name, "email": op.email,
        "station": body.station.value, "locationIds": location_ids,
        "tempPassword": op.temp_password,
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
    repo = get_repo()
    location_ids = _validated_location_ids(repo, merchant_id, body.locationIds)
    update_user(
        operation_user_id,
        email=str(body.email) if body.email is not None else None,
        station=body.station.value if body.station is not None else None,
        name=body.name,
    )
    if location_ids is not None:
        repo.set_operation_user_locations(operation_user_id, location_ids)
    else:
        location_ids = _location_ids_by_user(repo, merchant_id).get(operation_user_id, [])
    return {
        "id": operation_user_id,
        "name": body.name if body.name is not None else existing.get("name"),
        "email": str(body.email) if body.email is not None else existing.get("email"),
        "station": body.station.value if body.station is not None else existing.get("station"),
        "locationIds": location_ids,
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


# --- Locales (Phase 16) ---------------------------------------------------------------
def _clean_name(name: str) -> str:
    """The DB rejects a blank name (check constraint); say so as a 422, not a 500."""
    cleaned = name.strip()
    if not cleaned:
        raise ValidationFailed("location name is required",
                               details={"reason": "invalid_fields", "fields": ["name"]})
    return cleaned


def _round6(value: float) -> float:
    """numeric(9,6) column: store exactly what we'd read back."""
    return round(float(value), 6)


def _location_model(row: dict, op_user_count: int = 0) -> dict:
    return {
        "id": row["id"], "name": row["name"],
        "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
        "active": row["active"], "enrollmentQuery": f"local={row['id']}",
        "operationUserCount": op_user_count, "createdAt": row["created_at"],
    }


def _require_owner(merchant_id: str, identity: CurrentIdentity) -> None:
    identity.require_role("merchant_owner")
    identity.require_merchant(merchant_id)


def _limit_error() -> Conflict:
    return Conflict(
        f"at most {MAX_ACTIVE_LOCATIONS} locales can be active; pause another one first",
        details={"reason": "active_location_limit", "limit": MAX_ACTIVE_LOCATIONS})


def _require_location(repo: MerchantsRepository, merchant_id: str, location_id: str) -> dict:
    row = repo.get_location(merchant_id, location_id)
    if row is None:
        raise NotFound("location not found")
    return row


def _assignment_counts(repo: MerchantsRepository, merchant_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in repo.location_assignments(merchant_id):
        counts[row["location_id"]] = counts.get(row["location_id"], 0) + 1
    return counts


@app.get("/merchants/{merchant_id}/locations")
def list_locations(merchant_id: str, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    counts = _assignment_counts(repo, merchant_id)
    return [_location_model(r, counts.get(r["id"], 0))
            for r in repo.list_locations(merchant_id)]


@app.post("/merchants/{merchant_id}/locations", status_code=201)
def create_location(merchant_id: str, body: LocationCreate, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    active = True if body.active is None else body.active
    if active and repo.count_active_locations(merchant_id) >= MAX_ACTIVE_LOCATIONS:
        raise _limit_error()
    row = repo.create_location({
        "merchant_id": merchant_id, "name": _clean_name(body.name),
        "latitude": _round6(body.latitude), "longitude": _round6(body.longitude),
        "active": active,
    })
    _sync_classes(repo, merchant_id)
    return _location_model(row)


@app.patch("/merchants/{merchant_id}/locations/{location_id}")
def update_location(
    merchant_id: str, location_id: str, body: LocationUpdate, identity: CurrentIdentity,
):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    existing = _require_location(repo, merchant_id, location_id)
    patch: dict = {}
    if body.name is not None:
        patch["name"] = _clean_name(body.name)
    if body.latitude is not None:
        patch["latitude"] = _round6(body.latitude)
    if body.longitude is not None:
        patch["longitude"] = _round6(body.longitude)
    if body.active is not None:
        patch["active"] = body.active
    if (patch.get("active") and not existing["active"]
            and repo.count_active_locations(merchant_id) >= MAX_ACTIVE_LOCATIONS):
        raise _limit_error()
    row = repo.update_location(merchant_id, location_id, patch) if patch else existing
    if patch:
        _sync_classes(repo, merchant_id)
    return _location_model(row, _assignment_counts(repo, merchant_id).get(location_id, 0))


@app.delete("/merchants/{merchant_id}/locations/{location_id}", status_code=204)
def delete_location(merchant_id: str, location_id: str, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    _require_location(repo, merchant_id, location_id)
    repo.delete_location(merchant_id, location_id)
    _sync_classes(repo, merchant_id)
    return None


handler = lambda_handler(app)
