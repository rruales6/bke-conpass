"""merchants service — B2B onboarding, subscription, team, branches and plan changes.

Onboarding (Función 02, D21) creates the tenant (merchant + subscription + a pending `signup`
payment; price and limits come from the plan catalog) and provisions the owner's Supabase Auth
login (merchant_owner role). The access details are NOT e-mailed at signup any more: platform
admin checks the RUC and the receipt and activates the account, and the activation e-mail
carries them (D21). The owner must change the temporary password at first login (Función 03).

Team (operation users) is unlimited on every plan. Cashiers are managed by the owner and, on
Pro, by managers inside their branches (`staff.manager_scope`). Branches are limited by plan
(402 `tier_limit`, details.reason = location_limit). Plan changes (O4): an upgrade is requested,
paid by transfer and confirmed by an admin; a downgrade is scheduled for the next charge date.
"""
from __future__ import annotations

from datetime import UTC, datetime

from conpass_common import CurrentIdentity, billing, create_app, lambda_handler, wallet_sync
from conpass_common import staff as staff_scope
from conpass_common.assets import presign_payment_proof_upload
from conpass_common.demo import DEMO_PASSWORD
from conpass_common.email_templates import plan_change_payment_email
from conpass_common.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotConfigured,
    NotFound,
    TierLimit,
    ValidationFailed,
)
from conpass_common.logging import get_logger
from conpass_common.models import (
    BillingPeriod,
    LocationCreate,
    LocationUpdate,
    MerchantOnboardRequest,
    Method,
    OperationUserCreate,
    OperationUserUpdate,
    PaymentProofUploadRequest,
    PaymentSubmission,
    PlanChangeCreate,
)
from conpass_common.payment_settings import payment_settings_model
from conpass_common.plans import (
    PLANS,
    STAFF_ROLES,
    TIER_ORDER,
    current_pricing_phase,
    get_plan,
    next_tier_with_more_locations,
    plan_to_api,
    require_feature,
)
from conpass_common.providers import get_email_sender, get_payment_provider
from conpass_common.providers.payment import PaymentIntent
from conpass_common.provisioning import (
    delete_user,
    get_user_language,
    last_sign_ins,
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


@app.get("/payment-settings")
def get_payment_settings():
    """Public: the transfer/DEUNA account details shown on the signup page (Función
    02) — platform-admin maintains them in Función 04 (PATCH /admin/payment-settings)."""
    return payment_settings_model(get_repo().get_payment_settings())


@app.get("/plans")
def list_plans():
    """Public: the plan catalog (prices, inclusions, features) for the signup page and
    the in-app plan screen. Single source: conpass_common.plans, priced for the platform's
    current pricing phase (launch | list)."""
    phase = current_pricing_phase(get_repo().client)
    return [plan_to_api(PLANS[t], phase) for t in TIER_ORDER]


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


def _require_proof(payment: PaymentSubmission, hint: str) -> None:
    if payment.method in PROOF_REQUIRED_METHODS and not payment.proofStorageKey:
        raise ValidationFailed(
            f"payment.proofStorageKey is required for payment.method={payment.method.value} "
            f"— {hint}")


@app.post("/merchants", status_code=201)
def onboard_merchant(body: MerchantOnboardRequest):
    _require_proof(body.payment, "upload the receipt via POST /payment-proofs/upload-url first")
    proof_key = body.payment.proofStorageKey
    period = (body.billingPeriod or BillingPeriod.monthly).value
    tier = body.tier.value

    repo = get_repo()
    client = repo.client
    phase = current_pricing_phase(client)
    amount = billing.signup_amount(tier, period, phase)
    uploaded_at = datetime.now(UTC).isoformat() if proof_key else None

    merchant = repo.create_merchant({
        "business_name": body.businessName,
        "ruc": body.ruc,
        "category": body.category.value,
        "city": body.city,
        "contact_name": body.contactName,
        "contact_email": body.contactEmail,
        "contact_phone": body.contactPhone,
    })
    # The tenant is rolled back if anything that must exist with it fails (the cascade drops
    # the subscription, payment and activity rows).
    try:
        sub = repo.create_subscription(
            merchant["id"], tier, billing_period=period,
            payment_proof_key=proof_key, payment_proof_uploaded_at=uploaded_at)
        billing.create_payment(client, {
            "merchant_id": merchant["id"], "kind": "signup", "tier": tier,
            "billing_period": period, "amount_usd": float(amount),
            "method": body.payment.method.value, "status": "pending",
            "proof_storage_key": proof_key})
    except Exception:
        repo.delete_merchant(merchant["id"])
        raise

    # Record payment (stub: manual proof; platform-admin confirms activation).
    get_payment_provider().submit(PaymentIntent(
        merchant_id=merchant["id"], tier=tier, amount_usd=float(amount),
        method=body.payment.method.value, proof_storage_key=proof_key,
    ))

    # Provision the owner's login (Supabase Auth user with merchant_owner role). Roll back the
    # tenant if provisioning fails (e.g. email already in use). Nothing is e-mailed now: the
    # access details go out when platform-admin activates the account (D21).
    lang = body.language.value if body.language is not None else "es"
    try:
        owner = provision_user(
            email=str(body.contactEmail), roles=["merchant_owner"],
            merchant_id=merchant["id"], name=body.contactName,
            must_change_password=True, language=lang)
    except AppError:
        repo.delete_merchant(merchant["id"])
        raise

    billing.log_activity(client, merchant["id"], "signed_up", actor_user_id=owner.user_id,
                         actor_role="merchant_owner",
                         details={"tier": tier, "billingPeriod": period,
                                  "amountUsd": float(amount)})
    if proof_key:
        billing.log_activity(client, merchant["id"], "proof_uploaded",
                             actor_user_id=owner.user_id, actor_role="merchant_owner",
                             details={"kind": "signup"})

    sub_api = billing.subscription_for_api(client, sub, phase=phase, with_pending=False)
    return {
        "merchant": billing.merchant_to_api(merchant, sub_api),
        "subscription": sub_api,
        "ownerInviteSent": False,
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
    sub = repo.get_subscription(merchant_id)
    sub_api = billing.subscription_for_api(repo.client, sub) if sub else None
    return billing.merchant_to_api(m, sub_api)


@app.get("/merchants/{merchant_id}/usage")
def get_usage(merchant_id: str, identity: CurrentIdentity):
    """Plan consumption for the current billing month + the estimated invoice."""
    identity.require_merchant(merchant_id)   # owner of THIS merchant, or platform_admin
    return billing.usage_for(get_repo().client, merchant_id)


# --------------------------------------------------------------------------- #
# Team (operation users)
# --------------------------------------------------------------------------- #
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


def _team_scope(repo: MerchantsRepository, merchant_id: str, identity: CurrentIdentity, *,
                allow_admin: bool = False) -> list[str] | None:
    """Who may manage the team, and where.

    - None: unrestricted (the merchant's owner; platform admin only where `allow_admin`).
    - list: a manager, limited to these branch ids (`staff.manager_scope` re-reads the role, the
      plan and the branches from the DB; the token is only a hint).
    - Forbidden: a cashier, a manager no longer on Pro, a foreign tenant, anyone else.
    """
    identity.require_merchant(merchant_id)
    if "merchant_owner" in identity.roles:
        return None
    if "platform_admin" in identity.roles:
        if allow_admin:
            return None
        raise Forbidden("requires the owner or a manager")
    return staff_scope.manager_scope(repo.client, identity)


def _staff_role(row: dict) -> str:
    return row.get("staff_role") or "cashier"


def _require_in_scope(scope: list[str], location_ids: list[str]) -> None:
    outside = [i for i in location_ids if i not in set(scope)]
    if outside:
        raise Forbidden("a manager can only assign branches they manage",
                        details={"reason": "location_out_of_scope", "locationIds": outside})


def _operation_user_model(u: dict, location_ids: list[str],
                          last_sign_in: str | None = None) -> dict:
    out = {"id": u["user_id"], "name": u.get("name"), "email": u.get("email"),
           "station": u.get("station"), "staffRole": _staff_role(u),
           "locationIds": location_ids}
    if last_sign_in:
        out["lastSignInAt"] = last_sign_in
    return out


@app.get("/merchants/{merchant_id}/operation-users")
def list_operation_users(merchant_id: str, identity: CurrentIdentity):
    repo = get_repo()
    scope = _team_scope(repo, merchant_id, identity, allow_admin=True)
    users = repo.list_operation_users(merchant_id)
    assigned = _location_ids_by_user(repo, merchant_id)
    if scope is not None:
        # A manager sees the cashiers who work in at least one of their branches.
        mine = set(scope)
        users = [u for u in users if _staff_role(u) == "cashier"
                 and mine & set(assigned.get(u["user_id"], []))]
    sign_ins = last_sign_ins([u["user_id"] for u in users])
    return [_operation_user_model(u, assigned.get(u["user_id"], []), sign_ins.get(u["user_id"]))
            for u in users]


def _require_staff_roles_plan(repo: MerchantsRepository, merchant_id: str) -> None:
    sub = repo.get_subscription(merchant_id)
    if sub is None:
        raise NotFound("subscription not found")
    require_feature(sub["tier"], STAFF_ROLES)   # 402 feature_not_in_plan (Pro)


@app.post("/merchants/{merchant_id}/operation-users", status_code=201)
def add_operation_user(merchant_id: str, body: OperationUserCreate, identity: CurrentIdentity):
    repo = get_repo()
    identity.require_role("merchant_owner", "operation_user")
    scope = _team_scope(repo, merchant_id, identity)
    role = body.staffRole.value if body.staffRole is not None else "cashier"
    if role == "manager":
        if scope is not None:
            raise Forbidden("a manager can only create cashiers")
        _require_staff_roles_plan(repo, merchant_id)

    # Validate before provisioning so a bad locale never leaves an orphan login behind.
    location_ids = _validated_location_ids(repo, merchant_id, body.locationIds) or []
    if scope is not None:
        if not location_ids:
            raise ValidationFailed("assign at least one of your branches",
                                   details={"reason": "locations_required"})
        _require_in_scope(scope, location_ids)
    op = provision_user(
        email=str(body.email), roles=["operation_user"],
        merchant_id=merchant_id, station=body.station.value, name=body.name,
        staff_role=role)
    if location_ids:
        repo.set_operation_user_locations(op.user_id, location_ids)
    return {
        "id": op.user_id, "name": body.name, "email": op.email,
        "station": body.station.value, "staffRole": role, "locationIds": location_ids,
        "tempPassword": op.temp_password,
    }


def _require_operation_user(
    merchant_id: str, operation_user_id: str, identity: CurrentIdentity,
) -> tuple[dict, list[str] | None, list[str]]:
    """The target user, the caller's scope and the target's branches. A manager may only touch
    cashiers whose branches (at least one) are all inside their scope."""
    repo = get_repo()
    identity.require_role("merchant_owner", "operation_user")
    scope = _team_scope(repo, merchant_id, identity)
    existing = repo.get_operation_user(merchant_id, operation_user_id)
    if existing is None:
        raise NotFound("operation user not found")
    assigned = _location_ids_by_user(repo, merchant_id).get(operation_user_id, [])
    if scope is not None and (
            _staff_role(existing) != "cashier" or not assigned
            or not set(assigned) <= set(scope)):
        raise Forbidden("this user is outside the branches you manage")
    return existing, scope, assigned


@app.patch("/merchants/{merchant_id}/operation-users/{operation_user_id}")
def edit_operation_user(
    merchant_id: str, operation_user_id: str,
    body: OperationUserUpdate, identity: CurrentIdentity,
):
    existing, scope, assigned = _require_operation_user(
        merchant_id, operation_user_id, identity)
    repo = get_repo()
    new_role = body.staffRole.value if body.staffRole is not None else None
    if new_role == "manager":
        if scope is not None:
            raise Forbidden("a manager can only manage cashiers")
        _require_staff_roles_plan(repo, merchant_id)
    location_ids = _validated_location_ids(repo, merchant_id, body.locationIds)
    if scope is not None and location_ids is not None:
        if not location_ids:
            raise ValidationFailed("assign at least one of your branches",
                                   details={"reason": "locations_required"})
        _require_in_scope(scope, location_ids)
    update_user(
        operation_user_id,
        email=str(body.email) if body.email is not None else None,
        station=body.station.value if body.station is not None else None,
        name=body.name,
        staff_role=new_role,
    )
    if location_ids is not None:
        repo.set_operation_user_locations(operation_user_id, location_ids)
    else:
        location_ids = assigned
    return {
        "id": operation_user_id,
        "name": body.name if body.name is not None else existing.get("name"),
        "email": str(body.email) if body.email is not None else existing.get("email"),
        "station": body.station.value if body.station is not None else existing.get("station"),
        "staffRole": new_role or _staff_role(existing),
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


# --------------------------------------------------------------------------- #
# Locales (Phase 16) — limited by plan since v3.0
# --------------------------------------------------------------------------- #
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


def _location_model(row: dict, op_user_count: int = 0, stats: dict | None = None) -> dict:
    stats = stats or {}
    return {
        "id": row["id"], "name": row["name"],
        "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
        "active": row["active"], "enrollmentQuery": f"local={row['id']}",
        "operationUserCount": op_user_count,
        "enrolledClients": int(stats.get("enrolled_clients") or 0),
        "visits30d": int(stats.get("visits_30d") or 0),
        "createdAt": row["created_at"],
    }


def _require_owner(merchant_id: str, identity: CurrentIdentity) -> None:
    identity.require_role("merchant_owner")
    identity.require_merchant(merchant_id)


def _require_location_capacity(repo: MerchantsRepository, merchant_id: str) -> None:
    """402 when one more active branch would exceed the plan's limit (1 / 3 / 10)."""
    sub = repo.get_subscription(merchant_id)
    if sub is None:
        return
    tier = sub["tier"]
    limit = get_plan(tier).location_limit
    if repo.count_active_locations(merchant_id) >= limit:
        details = {"reason": "location_limit", "limit": limit}
        required = next_tier_with_more_locations(tier)
        if required is not None:
            details["requiredTier"] = required
        raise TierLimit(f"the {tier} plan allows {limit} active branch(es)", details=details)


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
    stats = repo.location_stats(merchant_id)    # one query for every branch
    return [_location_model(r, counts.get(r["id"], 0), stats.get(r["id"]))
            for r in repo.list_locations(merchant_id)]


@app.post("/merchants/{merchant_id}/locations", status_code=201)
def create_location(merchant_id: str, body: LocationCreate, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    active = True if body.active is None else body.active
    if active:
        _require_location_capacity(repo, merchant_id)
    row = repo.create_location({
        "merchant_id": merchant_id, "name": _clean_name(body.name),
        "latitude": _round6(body.latitude), "longitude": _round6(body.longitude),
        "active": active,
    })
    _sync_classes(repo, merchant_id)
    billing.log_activity(repo.client, merchant_id, "location_added",
                         actor_user_id=identity.user_id, actor_role="merchant_owner",
                         details={"locationId": row["id"], "name": row["name"]})
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
    if patch.get("active") and not existing["active"]:
        _require_location_capacity(repo, merchant_id)
    row = repo.update_location(merchant_id, location_id, patch) if patch else existing
    if patch:
        _sync_classes(repo, merchant_id)
    return _location_model(row, _assignment_counts(repo, merchant_id).get(location_id, 0),
                           repo.location_stats(merchant_id).get(location_id))


@app.delete("/merchants/{merchant_id}/locations/{location_id}", status_code=204)
def delete_location(merchant_id: str, location_id: str, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    _require_location(repo, merchant_id, location_id)
    repo.delete_location(merchant_id, location_id)
    _sync_classes(repo, merchant_id)
    return None


# --------------------------------------------------------------------------- #
# Payments (ledger) and plan changes (O4)
# --------------------------------------------------------------------------- #
def _require_owner_or_admin(merchant_id: str, identity: CurrentIdentity) -> None:
    """The merchant's owner or a platform admin (a cashier of the merchant is not enough)."""
    identity.require_role("merchant_owner", "platform_admin")
    identity.require_merchant(merchant_id)


@app.get("/merchants/{merchant_id}/payments")
def list_payments(merchant_id: str, identity: CurrentIdentity):
    _require_owner_or_admin(merchant_id, identity)
    return [billing.payment_to_api(p)
            for p in billing.list_payments(get_repo().client, merchant_id)]


@app.get("/merchants/{merchant_id}/payments/{payment_id}/receipt")
def get_payment_receipt(merchant_id: str, payment_id: str, identity: CurrentIdentity):
    _require_owner_or_admin(merchant_id, identity)
    return billing.payment_receipt_link(get_repo().client, merchant_id, payment_id)


def _owner_language(user_id: str | None) -> str:
    try:
        return get_user_language(user_id) if user_id else "es"
    except Exception:  # noqa: BLE001 - copy language is cosmetic; default to Spanish
        return "es"


def _email_upgrade_details(repo: MerchantsRepository, merchant: dict, identity: CurrentIdentity,
                           request_row: dict) -> None:
    """Best-effort: the amount + the platform's payment details. The console shows the same
    amount, so a delivery failure must not undo the request."""
    to = identity.email or merchant.get("contact_email")
    if not to:
        return
    try:
        subject, text, html = plan_change_payment_email(
            _owner_language(identity.user_id), owner_name=merchant.get("contact_name"),
            business_name=merchant["business_name"], tier=request_row["to_tier"],
            period=request_row["to_period"], amount_usd=request_row["amount_usd"],
            payment_settings=repo.get_payment_settings())
        get_email_sender().send(to, subject, text, html)
    except Exception as exc:  # noqa: BLE001 - EmailDeliveryError or a misconfigured sender
        log.error("plan_change_email_failed", extra={
            "merchant_id": merchant["id"], "error": type(exc).__name__})


@app.get("/merchants/{merchant_id}/plan-change")
def list_plan_changes(merchant_id: str, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    client = get_repo().client
    return billing.plan_changes_to_api(client, billing.list_plan_changes(client, merchant_id, 10))


@app.post("/merchants/{merchant_id}/plan-change", status_code=201)
def request_plan_change(merchant_id: str, body: PlanChangeCreate, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    repo = get_repo()
    client = repo.client
    merchant = repo.get_merchant(merchant_id)
    if merchant is None:
        raise NotFound("merchant not found")
    if merchant.get("is_demo"):
        raise Forbidden("the demo account cannot change plan")
    sub = repo.get_subscription(merchant_id)
    if sub is None:
        raise NotFound("subscription not found")

    from_tier, from_period = sub["tier"], sub.get("billing_period") or "monthly"
    to_tier, to_period = body.tier.value, body.billingPeriod.value
    direction = billing.plan_change_direction(from_tier, from_period, to_tier, to_period)
    if direction is None:
        raise Conflict("you are already on this plan",
                       details={"reason": "plan_change_same_plan"})
    if billing.open_plan_change(client, merchant_id) is not None:
        raise Conflict("there is already an open plan change request",
                       details={"reason": "plan_change_open"})

    row = {"merchant_id": merchant_id, "from_tier": from_tier, "to_tier": to_tier,
           "from_period": from_period, "to_period": to_period, "direction": direction,
           "requested_by": identity.user_id}
    if direction == "upgrade":
        phase = current_pricing_phase(client)
        amount = PLANS[to_tier].price_for(to_period, phase)
        payment = billing.create_payment(client, {
            "merchant_id": merchant_id, "kind": "plan_change", "tier": to_tier,
            "billing_period": to_period, "amount_usd": float(amount),
            "method": "manual_transfer", "status": "pending"})
        row.update(status="awaiting_payment", amount_usd=float(amount),
                   payment_id=payment["id"])
    else:
        effective = (billing.as_date(sub.get("next_charge_at"))
                     or billing.first_of_next_month(billing.today_utc()))
        row.update(status="scheduled", effective_at=effective.isoformat())
    created = client.table("plan_change_requests").insert(row).execute().data[0]

    billing.log_activity(client, merchant_id, "plan_change_requested",
                         actor_user_id=identity.user_id, actor_role="merchant_owner",
                         details={"direction": direction,
                                  "from": {"tier": from_tier, "billingPeriod": from_period},
                                  "to": {"tier": to_tier, "billingPeriod": to_period}})
    if direction == "upgrade":
        _email_upgrade_details(repo, merchant, identity, created)
    return billing.plan_changes_to_api(client, [created])[0]


def _own_request(client, merchant_id: str, request_id: str) -> dict:
    req = billing.get_plan_change(client, request_id)
    if req is None or req["merchant_id"] != merchant_id:
        raise NotFound("plan change request not found")
    return req


@app.delete("/merchants/{merchant_id}/plan-change/{request_id}", status_code=204)
def cancel_plan_change(merchant_id: str, request_id: str, identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    client = get_repo().client
    req = _own_request(client, merchant_id, request_id)
    if req["status"] not in billing.OPEN_STATUSES:
        raise Conflict("this request is already closed",
                       details={"reason": "plan_change_closed"})
    billing.update_plan_change(client, request_id,
                               {"status": "cancelled", "resolved_at": billing.now_iso()})
    if req.get("payment_id"):
        billing.reject_payment(client, req["payment_id"], "cancelled")
    return None


@app.post("/merchants/{merchant_id}/plan-change/{request_id}/proof")
def submit_plan_change_proof(merchant_id: str, request_id: str, body: PaymentSubmission,
                             identity: CurrentIdentity):
    _require_owner(merchant_id, identity)
    _require_proof(body, "upload the receipt via POST /payment-proofs/upload-url first")
    client = get_repo().client
    req = _own_request(client, merchant_id, request_id)
    if req["status"] != "awaiting_payment" or not req.get("payment_id"):
        raise Conflict("this request is not waiting for a payment",
                       details={"reason": "plan_change_not_awaiting_payment"})
    patch = {"method": body.method.value}
    if body.proofStorageKey:
        patch["proof_storage_key"] = body.proofStorageKey
    billing.update_payment(client, req["payment_id"], patch)
    updated = billing.update_plan_change(client, request_id, {"status": "pending_review"})
    billing.log_activity(client, merchant_id, "proof_uploaded", actor_user_id=identity.user_id,
                         actor_role="merchant_owner",
                         details={"kind": "plan_change", "requestId": request_id})
    return billing.plan_changes_to_api(client, [updated or {**req, "status": "pending_review"}])[0]


handler = lambda_handler(app)
