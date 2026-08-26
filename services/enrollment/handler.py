"""enrollment service — public customer enrollment → issue card (Función 01).

Critical B2C path: minimal PII (B6), idempotent on the Idempotency-Key and de-duplicated
on (program, dedupeKey) so a repeat submission from the same device returns the same
card. Wallet add-links are filled by the WalletProvider in Phase 4 (Google).
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, timedelta
from typing import Annotated

from conpass_common import create_app, lambda_handler
from conpass_common.errors import NotFound, ValidationFailed
from conpass_common.idempotency import IdempotencyStore, StoredResponse, run_idempotent
from conpass_common.models import EnrollmentRequest, Language
from conpass_common.providers import get_wallet_provider
from conpass_common.providers.wallet import build_pass_content
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


def _language(value: Language | str | None) -> str:
    """`language` defaults to a bare "es" string (pydantic does not validate a field's
    default value into its enum), while an explicit choice round-trips as a `Language`
    member — normalize both to the plain string the `cards.language` column stores."""
    return value.value if isinstance(value, Language) else (value or "es")


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
        content = build_pass_content(card, program, merchant, customer)
        provider = get_wallet_provider()
        link = provider.issue(content).add_link if mutate else provider.add_link(content)
        return {"google": link}
    except Exception:  # noqa: BLE001 - wallet is non-critical to enrollment
        log.warning("wallet link generation failed for card %s", card.get("id"),
                    exc_info=True)
        return {}


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
        if program is None or not program.get("active", True):
            raise NotFound("program not found or inactive")

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

        merchant_id = program["merchant_id"]
        customer = repo.create_customer({
            "merchant_id": merchant_id,
            "full_name": body.fullName,
            "email": body.email,
            "phone": body.phone,
            "birthday": body.birthday.isoformat() if body.birthday else None,
            "consent": bool(body.consent),
        })

        # Welcome bonus seeds the mechanic's balance; membership sets a validity window.
        welcome = program.get("welcome_bonus") or 0
        mechanic = program.get("mechanic")
        muntil = None
        if program["type"] == "membership_pass":
            days = _VALIDITY_DAYS.get(program.get("membership_validity") or "")
            muntil = (date.today() + timedelta(days=days)).isoformat() if days else None

        card = repo.create_card({
            "program_id": program_id,
            "merchant_id": merchant_id,
            "customer_id": customer["id"],
            "type": program["type"],
            "opaque_token": secrets.token_urlsafe(16),
            "stamps": welcome if mechanic == "stamps" else 0,
            "points": welcome if mechanic == "points" else 0,
            "holder_name": _holder_name(body.fullName),
            "membership_active_until": muntil,
            "dedupe_key": body.dedupeKey,
            "language": _language(body.language),
        })
        links = _wallet_links(repo, card, program, customer, mutate=True)
        return StoredResponse(201, _card_issued(card, program, links))

    result = run_idempotent(get_idempotency_store(), key=idempotency_key,
                            endpoint="enroll", payload=body.model_dump(mode="json"),
                            compute=compute)
    return JSONResponse(status_code=result.status, content=result.body)


handler = lambda_handler(app)
