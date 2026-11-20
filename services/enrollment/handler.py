"""enrollment service — public customer enrollment → issue card (Función 01).

Critical B2C path: minimal PII (B6), idempotent on the Idempotency-Key and de-duplicated
on (program, dedupeKey) so a repeat submission from the same device returns the same
card. Wallet add-links are filled by the WalletProvider in Phase 4 (Google).
"""
from __future__ import annotations

import calendar
import logging
import secrets
from datetime import date, timedelta
from enum import Enum
from typing import Annotated

from conpass_common import create_app, lambda_handler, wallet_sync
from conpass_common.assets import public_asset_url
from conpass_common.campaigns import SEASONAL, STATUS_DISABLED, campaign_status, require_active
from conpass_common.card_links import verify_birthday_link_token
from conpass_common.cards import card_flags, new_opaque_token
from conpass_common.db import service_client
from conpass_common.errors import NotFound, ValidationFailed
from conpass_common.idempotency import IdempotencyStore, StoredResponse, run_idempotent
from conpass_common.locations import active_locations
from conpass_common.models import CardBirthdayUpdate, EnrollmentRequest
from conpass_common.providers import get_wallet_provider
from fastapi import Header
from fastapi.responses import JSONResponse

from .repository import EnrollmentRepository

log = logging.getLogger(__name__)
app = create_app(service="enrollment")

_VALIDITY_DAYS = {"monthly": 30, "quarterly": 90, "annual": 365, "per_event": None}


def get_repo() -> EnrollmentRepository:
    return EnrollmentRepository()


def get_idempotency_store() -> IdempotencyStore:
    return IdempotencyStore()


def _language(value: Enum | str | None) -> str:
    """`language` defaults to a bare "es" string (pydantic does not validate a field's
    default value into its enum), while an explicit choice round-trips as a `Language`
    member — normalize both to the plain string the `cards.language` column stores."""
    # Duck-typed on purpose: codegen numbers inline enums (Language, Language1, ...) and
    # renumbers them whenever another schema adds one, so never import it by name.
    return value.value if isinstance(value, Enum) else (value or "es")


def _holder_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _card_issued(card: dict, program: dict, wallet_links: dict | None = None) -> dict:
    mechanic = program.get("mechanic") or "stamps"
    return {
        "card": {
            "id": card["id"], "programId": card["program_id"],
            "merchantId": card["merchant_id"], "customerId": card.get("customer_id"),
            "type": card["type"], "opaqueToken": card["opaque_token"],
            "balance": {
                "mechanic": mechanic,
                "stamps": card.get("stamps", 0),
                "stampsForReward": program.get("stamps_for_reward"),
                "points": card.get("points", 0),
                "pointsForReward": program.get("points_for_reward"),
                "rewardsAvailable": card.get("rewards_available", 0),
                "rewardsRedeemed": card.get("rewards_redeemed", 0),
                "membershipActiveUntil": card.get("membership_active_until"),
            },
            "holderName": card.get("holder_name"),
            "language": card.get("language", "es"),
            "active": card.get("active", True),
            "walletInstalled": card.get("wallet_installed", False),
            **card_flags(card),
            "createdAt": card["created_at"],
        },
        "walletLinks": wallet_links or {},
    }


def _wallet_links(repo: EnrollmentRepository, card: dict, program: dict,
                  customer: dict | None, *, mutate: bool) -> dict:
    """Best-effort "Add to Google Wallet" link. A wallet failure must never block
    enrollment (backend is authority), so any error degrades to no links. `mutate`
    issues/refreshes the pass object (new card); otherwise only re-mints the link
    (dedupe replay), avoiding a redundant write. `customer` supplies the pass's contact
    line (email) — the caller passes the row it already has, or None if it's genuinely
    unavailable, so this never needs its own read on the hot enrollment path.
    """
    try:
        merchant = repo.get_merchant(card["merchant_id"])
        # Through wallet_sync: issue() re-PUTs the program class, so the content must
        # carry the merchant's nearby locales or every enrolment would wipe them.
        content = wallet_sync.content_for(None, card, program, merchant, customer)
        provider = get_wallet_provider()
        link = provider.issue(content).add_link if mutate else provider.add_link(content)
        return {"google": link}
    except Exception:  # noqa: BLE001 - wallet is non-critical to enrollment
        log.warning("wallet link generation failed for card %s", card.get("id"),
                    exc_info=True)
        return {}


def _enrolled_location_id(merchant_id: str, location_id) -> str | None:
    """The `?local=` attribution, kept only when it is an active locale of the program's
    merchant. Silent otherwise: a stale or foreign link must never block a sign-up."""
    if location_id is None:
        return None
    try:
        wanted = str(location_id)
        if any(r["id"] == wanted for r in active_locations(service_client(), merchant_id)):
            return wanted
    except Exception:  # noqa: BLE001 - attribution is optional
        log.warning("enrolled location lookup failed", exc_info=True)
    return None


@app.get("/programs/{program_id}/enroll")
def enroll_preview(program_id: str):
    """Public (Phase 18, board 09): the card the customer is about to save. Membership passes
    are parked (owner) and answer 404 like an unknown program."""
    repo = get_repo()
    p = repo.get_program(program_id)
    if p is None or p.get("type") == "membership_pass":
        raise NotFound("program not found")
    merchant = repo.get_merchant(p["merchant_id"]) or {}
    loyalty = p["type"] in ("loyalty_stamps", "loyalty_points")
    out = {
        "programId": p["id"], "businessName": merchant.get("business_name") or "",
        "programName": p["name"], "type": p["type"], "mechanic": p.get("mechanic"),
        "stampsForReward": p.get("stamps_for_reward"),
        "pointsForReward": p.get("points_for_reward"),
        "pointsPerDollar": float(p["points_per_dollar"]) if p.get("points_per_dollar") else None,
        "reward": p.get("reward"), "occasion": p.get("occasion"),
        "discountPercent": p.get("discount_percent"),
        "validFrom": str(p["valid_from"])[:10] if p.get("valid_from") else None,
        "validUntil": str(p["valid_until"])[:10] if p.get("valid_until") else None,
        "campaignStatus": campaign_status(p), "color": p.get("color"),
        "logoUrl": public_asset_url(p.get("icon_storage_key")),
        "backgroundUrl": public_asset_url(p.get("background_storage_key")),
        "birthdayOffer": bool(loyalty and repo.birthday_offer(p["merchant_id"])),
    }
    return {k: v for k, v in out.items() if v is not None}


@app.post("/programs/{program_id}/enroll")
def enroll_customer(
    program_id: str,
    body: EnrollmentRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if not idempotency_key:
        raise ValidationFailed("Idempotency-Key header is required")
    repo = get_repo()

    def compute() -> StoredResponse:
        program = repo.get_program(program_id)
        if program is None:
            raise NotFound("program not found")
        # A switched-off campaign never enrols (also on replay, as before). Before this
        # check was a 404 "inactive"; it is now the contract's 422 campaign_not_active.
        if campaign_status(program) == STATUS_DISABLED:
            require_active(program)

        # De-dup a repeat submission from the same device.
        if body.dedupeKey:
            existing = repo.find_card_by_dedupe(program_id, body.dedupeKey)
            if existing:
                # Replay path: the customer wasn't just created, so fetch it (may be
                # None for an old anonymous enrollment with no customer_id).
                existing_customer = (repo.get_customer(existing["customer_id"])
                                     if existing.get("customer_id") else None)
                links = _wallet_links(repo, existing, program, existing_customer,
                                      mutate=False)
                return StoredResponse(200, _card_issued(existing, program, links))

        # New cards only while the campaign is active (upcoming / ended seasonal windows
        # refuse). The dedupe replay above is deliberately exempt: the customer already
        # holds this card, so a repeat visit to the link still returns it after the end.
        require_active(program)

        merchant_id = program["merchant_id"]
        customer = repo.create_customer({
            "merchant_id": merchant_id,
            "full_name": body.fullName,
            "email": body.email,
            "phone": body.phone,
            # D15: only day + month are kept; the year is dropped here.
            "birthday_day": body.birthday.day if body.birthday else None,
            "birthday_month": body.birthday.month if body.birthday else None,
            "consent": body.consent is True,      # absent -> false (never pre-ticked)
        })

        # Welcome bonus seeds the mechanic's balance; membership sets a validity window.
        welcome = program.get("welcome_bonus") or 0
        mechanic = program.get("mechanic")
        muntil = None
        if program["type"] == "membership_pass":
            days = _VALIDITY_DAYS.get(program.get("membership_validity") or "")
            muntil = (date.today() + timedelta(days=days)).isoformat() if days else None

        card_data = {
            "program_id": program_id,
            "merchant_id": merchant_id,
            "customer_id": customer["id"],
            "type": program["type"],
            "opaque_token": new_opaque_token(),
            "stamps": welcome if mechanic == "stamps" else 0,
            "points": welcome if mechanic == "points" else 0,
            "holder_name": _holder_name(body.fullName),
            "membership_active_until": muntil,
            "dedupe_key": body.dedupeKey,
            "language": _language(body.language),
            # A seasonal card is born with its single discount available.
            "rewards_available": 1 if program["type"] == SEASONAL else 0,
        }
        enrolled_at = _enrolled_location_id(merchant_id, body.locationId)
        if enrolled_at:
            card_data["enrolled_location_id"] = enrolled_at
        card = repo.create_card(card_data)
        links = _wallet_links(repo, card, program, customer, mutate=True)
        return StoredResponse(201, _card_issued(card, program, links))

    result = run_idempotent(get_idempotency_store(), key=idempotency_key,
                            endpoint="enroll", payload=body.model_dump(mode="json"),
                            compute=compute)
    return JSONResponse(status_code=result.status, content=result.body)


@app.put("/programs/{program_id}/enroll/birthday", status_code=204)
def set_card_birthday(program_id: str, body: CardBirthdayUpdate):
    """Public. The card's opaque token (the QR secret), or the signed token of the pass-back
    birthday link (card_links, D15), authorises it. Every failed check is
    the same 404, so nothing reveals whether the card, the program or the token was wrong."""
    repo = get_repo()
    card = repo.get_card(str(body.cardId))
    if (card is None or card.get("program_id") != program_id
            or not (secrets.compare_digest(str(card.get("opaque_token") or "").encode(),
                                           body.token.encode())
                    or verify_birthday_link_token(str(body.cardId), body.token))):
        raise NotFound("card not found")
    day, month = body.day, body.month
    if (day is None) != (month is None):
        raise ValidationFailed("day and month must be sent together",
                               details={"reason": "invalid_birthday"})
    if day is not None and month is not None and not (
            1 <= month <= 12 and 1 <= day <= calendar.monthrange(2024, month)[1]):
        # 2024 is a leap year: 29 February is accepted.
        raise ValidationFailed("that day does not exist in that month",
                               details={"reason": "invalid_birthday"})
    patch = {"birthday_day": day, "birthday_month": month}
    customer_id = card.get("customer_id")
    if customer_id:
        repo.update_customer(customer_id, patch)
    elif day is not None:
        # An anonymous card (no customer row): create one for this merchant and link it.
        customer = repo.create_customer({"merchant_id": card["merchant_id"],
                                         "consent": False, **patch})
        repo.link_customer(card["id"], customer["id"])
    return None


handler = lambda_handler(app)
