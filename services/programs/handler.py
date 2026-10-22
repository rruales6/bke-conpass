"""programs service — program config & metrics (Función 05)."""
from __future__ import annotations

import logging

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.assets import presign_upload, public_asset_url
from conpass_common.campaigns import (
    ACCUMULATION_TYPES,
    SEASONAL,
    campaign_fields,
    inactivity_days,
    inactivity_fields,
    validate_seasonal,
)
from conpass_common.db import service_client
from conpass_common.errors import Forbidden, NotFound, ValidationFailed
from conpass_common.models import AppearanceUploadRequest, ProgramCreate, ProgramUpdate
from conpass_common.providers import get_wallet_provider
from conpass_common.providers.wallet import build_pass_content

from .repository import ProgramsRepository

log = logging.getLogger(__name__)

ENROLL_BASE = "https://conpass.cards/enroll"

# An appearance edit has to reach passes that are already installed, and each pass is a
# separate Google API call inside a 15s Lambda. Bounded so a large programme cannot blow
# the request budget; the rest are picked up the next time each card is operated on.
# (Moving this to an async worker is the P1 "async wallet push" backlog item.)
WALLET_PUSH_MAX_CARDS = 40

# Program columns that are mirrored onto the wallet pass.
# membership_includes now renders as the membership pass's "includes" row (Phase 12) —
# ProgramUpdate doesn't expose it for editing yet, so this entry is inert today, but it
# keeps the allowlist correct for the day it does.
_WALLET_VISIBLE = ("name", "color", "icon_storage_key", "background_storage_key",
                   "stamps_for_reward", "points_for_reward", "reward",
                   "membership_includes",
                   # Seasonal campaign fields shown on the pass (occasion / discount row,
                   # validity window).
                   "occasion", "discount_percent", "valid_from", "valid_until")

app = create_app(service="programs")


def get_repo() -> ProgramsRepository:
    return ProgramsRepository()


def _appearance_patch(ap) -> dict:
    """Map an incoming ProgramAppearance onto DB columns.

    Empty string clears a field (-> NULL); a field left unset (None) keeps whatever the
    row already has. Colour and background image COMBINE — the colour is the pass
    background and the image is drawn on it — so there is nothing to reject here: Google
    Wallet does not derive a colour from the hero image, it falls back to its own default,
    so a merchant using an image still needs the colour to be theirs.
    """
    patch: dict = {}
    if ap.color is not None:
        patch["color"] = ap.color or None
    if ap.iconStorageKey is not None:
        patch["icon_storage_key"] = ap.iconStorageKey or None
    if ap.backgroundStorageKey is not None:
        patch["background_storage_key"] = ap.backgroundStorageKey or None
    return patch


def _program_model(p: dict) -> dict:
    return {
        "id": p["id"], "merchantId": p["merchant_id"], "type": p["type"],
        "name": p["name"], "mechanic": p.get("mechanic"),
        "stampsForReward": p.get("stamps_for_reward"),
        "pointsForReward": p.get("points_for_reward"),
        "pointsPerDollar": (float(p["points_per_dollar"])
                            if p.get("points_per_dollar") is not None else None),
        "reward": p.get("reward"),
        "membershipValidity": p.get("membership_validity"),
        "membershipIncludes": p.get("membership_includes"),
        "welcomeBonus": p.get("welcome_bonus", 0),
        "expiryDays": p.get("expiry_days"),
        **inactivity_fields(p),
        **campaign_fields(p),
        "appearance": {
            "color": p.get("color"),
            "iconStorageKey": p.get("icon_storage_key"),
            "backgroundStorageKey": p.get("background_storage_key"),
            "iconUrl": public_asset_url(p.get("icon_storage_key")),
            "backgroundUrl": public_asset_url(p.get("background_storage_key")),
        },
        "wallets": p.get("wallets") or [],
        "active": p.get("active", True),
        "enrollmentUrl": f"{ENROLL_BASE}/{p['id']}",
        "createdAt": p["created_at"],
    }


@app.get("/programs")
def list_programs(identity: CurrentIdentity, merchantId: str | None = None):
    merchant_id = merchantId or identity.merchant_id
    if merchant_id is None:
        raise NotFound("no merchant in scope")
    identity.require_merchant(merchant_id)
    return [_program_model(p) for p in get_repo().list(merchant_id)]


@app.post("/programs", status_code=201)
def create_program(body: ProgramCreate, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    merchant_id = identity.merchant_id
    if merchant_id is None:
        raise Forbidden("no merchant in scope")
    repo = get_repo()
    # No program limit on any plan (Phase 13) — only active cards are metered.
    appearance = _appearance_patch(body.appearance)
    seasonal = body.type.value == SEASONAL
    seasonal_cols = {
        "occasion": (body.occasion or "").strip() or None,
        "discount_percent": body.discountPercent,
        "valid_from": body.validFrom.isoformat() if body.validFrom else None,
        "valid_until": body.validUntil.isoformat() if body.validUntil else None,
    } if seasonal else {
        "occasion": None, "discount_percent": None, "valid_from": None, "valid_until": None,
    }
    if seasonal:
        # Seasonal campaigns are on every plan (no feature gate) and have no mechanic.
        validate_seasonal({**seasonal_cols, "reward": body.reward})
    program = repo.create({
        "merchant_id": merchant_id,
        "type": body.type.value,
        "name": body.name,
        "mechanic": (body.mechanic.value if body.mechanic and not seasonal else None),
        "stamps_for_reward": body.stampsForReward,
        "points_for_reward": body.pointsForReward,
        "points_per_dollar": body.pointsPerDollar,
        "reward": body.reward,
        "membership_validity": body.membershipValidity.value if body.membershipValidity else None,
        "membership_includes": body.membershipIncludes,
        "welcome_bonus": body.welcomeBonus or 0,
        "expiry_days": body.expiryDays,
        # Accumulation campaigns only; NULL means "the 90-day default".
        "inactivity_days": (body.inactivityDays
                            if body.type.value in ACCUMULATION_TYPES else None),
        "color": appearance.get("color"),
        "icon_storage_key": appearance.get("icon_storage_key"),
        "background_storage_key": appearance.get("background_storage_key"),
        "wallets": [w.value for w in body.wallets],
        **seasonal_cols,
    })
    return _program_model(program)


@app.get("/programs/{program_id}")
def get_program(program_id: str, identity: CurrentIdentity):
    p = get_repo().get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])
    return _program_model(p)


@app.patch("/programs/{program_id}")
def update_program(program_id: str, body: ProgramUpdate, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    repo = get_repo()
    p = repo.get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])

    patch: dict = {}
    if body.name is not None:
        patch["name"] = body.name
    if body.reward is not None:
        patch["reward"] = body.reward
    if body.stampsForReward is not None:
        patch["stamps_for_reward"] = body.stampsForReward
    if body.pointsForReward is not None:
        patch["points_for_reward"] = body.pointsForReward
    if body.pointsPerDollar is not None:
        patch["points_per_dollar"] = body.pointsPerDollar
    if body.inactivityDays is not None and p.get("type") in ACCUMULATION_TYPES:
        patch["inactivity_days"] = body.inactivityDays
    if p.get("type") == SEASONAL:
        if body.occasion is not None:
            patch["occasion"] = body.occasion.strip() or None
        if body.discountPercent is not None:
            patch["discount_percent"] = body.discountPercent
        if body.validFrom is not None:
            patch["valid_from"] = body.validFrom.isoformat()
        if body.validUntil is not None:
            patch["valid_until"] = body.validUntil.isoformat()
        # The merged row must still be a valid seasonal campaign (only re-checked when
        # an offer/date field is touched, so unrelated edits to a legacy row still work).
        if any(c in patch for c in ("occasion", "discount_percent", "valid_from",
                                    "valid_until", "reward")):
            validate_seasonal({**p, **patch})
    if body.active is not None:
        patch["active"] = body.active
    if body.appearance is not None:
        patch.update(_appearance_patch(body.appearance))
    if not patch:
        return _program_model(p)
    # Snapshot before the write: `updated` may be the same row object we were handed.
    before = {col: p.get(col) for col in _WALLET_VISIBLE}
    updated = repo.update(program_id, patch)
    if any(patch[col] != before[col] for col in _WALLET_VISIBLE if col in patch):
        _push_appearance_to_passes(repo, updated)
    return _program_model(updated)


@app.post("/programs/{program_id}/appearance-upload-url")
def create_appearance_upload_url(
    program_id: str, body: AppearanceUploadRequest, identity: CurrentIdentity,
):
    identity.require_role("merchant_owner")
    p = get_repo().get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])
    try:
        return presign_upload(
            program_id=program_id,
            kind=body.kind.value,
            content_type=body.contentType.value,
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _push_appearance_to_passes(repo: ProgramsRepository, program: dict) -> None:
    """Reflect a program edit into already-issued passes.

    Best-effort, exactly like the cashier's post-commit push: the program change is
    already committed and authoritative, so a wallet outage must not fail the request.
    Without this, colour/logo/hero-image and reward-label edits only ever reached passes
    issued *after* the edit — an installed pass kept whatever it was created with.
    """
    try:
        cards = repo.list_card_rows(str(program["id"]), WALLET_PUSH_MAX_CARDS)
        if not cards:
            return
        merchant = repo.get_merchant(str(program["merchant_id"]))
        provider = get_wallet_provider()
        try:
            # Class-level template (card-front row layout) lives once per program, not
            # once per card — refresh it before touching any individual pass. Any one
            # of the batch's cards works: the fields sync_program reads are all
            # program/merchant-level, not per-card.
            provider.sync_program(build_pass_content(cards[0], program, merchant))
        except Exception:  # noqa: BLE001 - wallet reflection is non-critical
            log.warning("wallet program sync failed for program %s",
                        program.get("id"), exc_info=True)
        # One query for every card's customer instead of one per card (up to
        # WALLET_PUSH_MAX_CARDS queries) — this loop already runs inside a 15s Lambda.
        customer_ids = [c["customer_id"] for c in cards if c.get("customer_id")]
        customers = repo.get_customers_by_ids(customer_ids)
        for card in cards:
            try:
                customer = customers.get(card.get("customer_id"))
                provider.update(build_pass_content(card, program, merchant, customer))
            except Exception:  # noqa: BLE001 - one bad pass must not stop the rest
                log.warning("wallet appearance push failed for card %s",
                            card.get("id"), exc_info=True)
        total = repo.count_cards(str(program["id"]))
        if total > WALLET_PUSH_MAX_CARDS:
            log.warning(
                "program %s has %d cards; refreshed the first %d, the rest update on "
                "their next operation", program["id"], total, WALLET_PUSH_MAX_CARDS)
    except Exception:  # noqa: BLE001 - wallet reflection is non-critical
        log.warning("wallet appearance push failed for program %s",
                    program.get("id"), exc_info=True)


def _program_metrics_model(row: dict | None, program: dict) -> dict:
    """Metrics payload. churnRate (abandonment) / inactivityDays exist for accumulation
    campaigns only: they are left out of the dict, not set to null, so the JSON omits
    them for a membership pass."""
    accumulation = program.get("type") in ACCUMULATION_TYPES
    if row is None:
        out = {
            "visits": 0, "redemptions": 0, "activeInstalledPasses": 0,
            "installsThisWeek": 0, "eligibleForReminder": 0,
            "secondVisitRate30d": 0.0,
        }
        if accumulation:
            out["churnRate"] = 0.0
            out["inactivityDays"] = inactivity_days(program)
        return out
    out = {
        "visits": row.get("visits", 0),
        "redemptions": row.get("redemptions", 0),
        "activeInstalledPasses": row.get("active_installed_passes", 0),
        "installsThisWeek": row.get("installs_this_week", 0),
        "eligibleForReminder": row.get("eligible_for_reminder", 0),
        "secondVisitRate30d": float(row.get("second_visit_rate_30d") or 0),
    }
    # The view yields NULL for both on a membership pass; omit rather than emit null.
    if accumulation and row.get("churn_rate") is not None:
        out["churnRate"] = float(row["churn_rate"])
    if accumulation:
        out["inactivityDays"] = (int(row["inactivity_days"])
                                 if row.get("inactivity_days") is not None
                                 else inactivity_days(program))
    return out


@app.get("/programs/{program_id}/metrics")
def get_program_metrics(program_id: str, identity: CurrentIdentity):
    p = get_repo().get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])
    rows = (service_client().table("program_metrics_view").select("*")
            .eq("program_id", program_id).execute().data)
    return _program_metrics_model(rows[0] if rows else None, p)


handler = lambda_handler(app)
