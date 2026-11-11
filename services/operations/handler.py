"""operations service — in-store accrue / redeem / validate / resolve.

Single-responsibility Lambda. Mutations are idempotent (offline-replayable, B5) and
enforce a per-window fraud guard. Balance changes are backend-authoritative; after a
successful commit the new balance is reflected into the wallet best-effort (Phase 4) —
a wallet failure never fails the operation, and replays don't re-push.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from conpass_common import CurrentIdentity, create_app, lambda_handler, locations, wallet_sync
from conpass_common.campaigns import SEASONAL, campaign_fields, inactivity_fields, require_active
from conpass_common.db import service_client
from conpass_common.errors import (
    Conflict,
    DiscountAlreadyUsed,
    NotFound,
    RateLimited,
    ValidationFailed,
)
from conpass_common.idempotency import IdempotencyStore, StoredResponse, run_idempotent
from conpass_common.models import (
    AccessDecision,
    AccrueRequest,
    CardResolution,
    OperationResult,
    RedeemRequest,
    ScanResolveRequest,
    ValidateAccessRequest,
)
from conpass_common.notify.automations import AccrualEvent, after_accrual
from fastapi import Header, Query

from . import logic
from .logic import CardState
from .repository import CardRow, OperationsRepository, SupabaseRepository, TxnRow

log = logging.getLogger(__name__)
# Card's enrollment link base (mirrors the programs service).
ENROLL_BASE = "https://conpass.cards/e"

app = create_app(service="operations")

# Fraud guard: at most N accruals per card per window (B5). Tunable per program later.
ACCRUAL_WINDOW_SECONDS = 60
MAX_ACCRUALS_PER_WINDOW = 5


def get_repo() -> OperationsRepository:
    return SupabaseRepository()


def get_idempotency_store() -> IdempotencyStore:
    return IdempotencyStore()


def _now() -> datetime:
    return datetime.now(UTC)


def _require_key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise ValidationFailed("Idempotency-Key header is required")
    return idempotency_key


def _operation_result(card: CardRow, txn: TxnRow) -> dict:
    return OperationResult.model_validate({
        "card": {
            "id": card.id, "programId": card.program_id, "merchantId": card.merchant_id,
            "type": card.program_type,
            "balance": {
                "mechanic": "stamps" if card.program_type == "loyalty_stamps" else "points",
                "stamps": card.state.stamps, "points": card.state.points,
                "rewardsAvailable": card.state.rewards_available,
                "rewardsRedeemed": card.state.rewards_redeemed,
            },
            "active": card.state.active, "createdAt": _now().isoformat(),
        },
        "transaction": {
            "id": txn.id, "cardId": txn.card_id, "kind": txn.kind,
            "stampsDelta": txn.stamps_delta, "pointsDelta": txn.points_delta,
            "operationUserId": txn.operation_user_id, "locationId": txn.location_id,
            "createdAt": txn.created_at.isoformat(),
        },
        "walletUpdateQueued": True,
    }).model_dump(by_alias=True, exclude_none=True)


def _program_model(p: dict) -> dict:
    # Program schema requires >=1 wallet; single-issuer default is google.
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
        },
        "wallets": p.get("wallets") or ["google"],
        "active": p.get("active", True),
        "enrollmentUrl": f"{ENROLL_BASE}/{p['id']}",
        "createdAt": p["created_at"],
    }


def _card_model(c: dict, program: dict) -> dict:
    mechanic = program.get("mechanic") or (
        "points" if c["type"] == "loyalty_points" else "stamps")
    return {
        "id": c["id"], "programId": c["program_id"], "merchantId": c["merchant_id"],
        "customerId": c.get("customer_id"), "type": c["type"],
        "opaqueToken": c["opaque_token"],
        "balance": {
            "mechanic": mechanic,
            "stamps": c.get("stamps", 0),
            "stampsForReward": program.get("stamps_for_reward"),
            "points": c.get("points", 0),
            "pointsForReward": program.get("points_for_reward"),
            "rewardsAvailable": c.get("rewards_available", 0),
            "rewardsRedeemed": c.get("rewards_redeemed", 0),
            "membershipActiveUntil": c.get("membership_active_until"),
        },
        "holderName": c.get("holder_name"),
        "active": c.get("active", True),
        "walletInstalled": c.get("wallet_installed", False),
        "createdAt": c["created_at"],
    }


def _push_wallet_update(repo: OperationsRepository, card_id: str) -> None:
    """Reflect a just-committed balance change into the installed pass. Best-effort:
    a wallet failure must never fail the (backend-authoritative) in-store operation,
    and this runs after commit so it can't extend the transaction. The pass is rebuilt
    from the DB by wallet_sync (balances, level, messages, locations)."""
    try:
        wallet_sync.push_card(service_client(), card_id)
    except Exception:  # noqa: BLE001 - wallet reflection is non-critical
        log.warning("wallet update failed for card %s", card_id, exc_info=True)


def _require_active(card: CardRow) -> None:
    """A deactivated card (owner action, or a public-demo card past its 7 days) can neither
    accrue nor redeem. The reason lets the cashier say so instead of a generic error."""
    if not card.state.active:
        raise ValidationFailed("card is inactive", details={"reason": "card_inactive"})


def _resolve_location(identity, merchant_id: str, location_id) -> str | None:
    """The scan's locale: None when omitted (a merchant without locales); else checked
    (active, the merchant's, assigned to a cashier) — 422 propagates."""
    if location_id is None:
        return None
    return locations.resolve_scan_location(service_client(), identity, merchant_id,
                                           str(location_id))


def _enqueue_event_automation(card: CardRow, before: CardState, outcome,
                              rules: logic.ProgramRules) -> None:
    """After an accrual commits: queue at most one wallet notification (ready > milestone
    > near > balance). Best-effort — a failure is logged and never reaches the cashier."""
    try:
        stamps = rules.mechanic == "stamps"
        target = rules.stamps_for_reward if stamps else rules.points_for_reward
        after_held = card.state.stamps if stamps else card.state.points
        before_held = before.stamps if stamps else before.points
        after_accrual(service_client(), AccrualEvent(
            card_id=card.id, merchant_id=card.merchant_id,
            mechanic="stamps" if stamps else "points",
            before_held=before_held, after_held=after_held,
            gained=outcome.stamps_delta if stamps else outcome.points_delta,
            target=target, rewards_earned=outcome.rewards_earned, accrual_count=0))
    except Exception:  # noqa: BLE001 - notifications never fail an accrual
        log.warning("event automation failed for card %s", card.id, exc_info=True)


@app.post("/operations/resolve")
def resolve_scan(body: ScanResolveRequest, identity: CurrentIdentity):
    identity.require_role("merchant_owner", "operation_user")
    repo = get_repo()
    card = repo.get_card_row_by_token(body.code)
    if card is None:
        raise NotFound("card not found for code")
    identity.require_merchant(card["merchant_id"])
    program = repo.get_program(card["program_id"])
    if program is None:
        raise NotFound("program not found")
    return CardResolution.model_validate({
        "card": _card_model(card, program),
        "program": _program_model(program),
    }).model_dump(by_alias=True, exclude_none=True)


@app.post("/operations/accrue")
def accrue(
    body: AccrueRequest,
    identity: CurrentIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    identity.require_role("merchant_owner", "operation_user")
    key = _require_key(idempotency_key)
    repo = get_repo()

    def compute() -> StoredResponse:
        card = repo.get_card(str(body.cardId))
        if card is None:
            raise NotFound("card not found")
        identity.require_merchant(card.merchant_id)
        _require_active(card)
        rules = repo.get_rules(card.program_id)
        if rules is None:
            raise NotFound("program not found")
        location_id = _resolve_location(identity, card.merchant_id, body.locationId)
        if repo.recent_accrual_count(card.id, ACCRUAL_WINDOW_SECONDS) >= MAX_ACCRUALS_PER_WINDOW:
            raise RateLimited("too many accruals in the fraud window")
        try:
            outcome = logic.accrue(rules, card.state, stamps=body.stamps, amount=body.amount)
        except logic.RewardMechanicError as exc:
            raise ValidationFailed(str(exc)) from exc
        before = card.state
        card.state = outcome.state
        txn = TxnRow(str(uuid.uuid4()), card.id, outcome.kind, outcome.stamps_delta,
                     outcome.points_delta, str(body.operationUserId), _now(), location_id)
        repo.commit(card, txn)
        _push_wallet_update(repo, card.id)
        _enqueue_event_automation(card, before, outcome, rules)
        return StoredResponse(200, _operation_result(card, txn))

    return run_idempotent(get_idempotency_store(), key=key, endpoint="accrue",
                          payload=body.model_dump(mode="json"), compute=compute).body


@app.post("/operations/redeem")
def redeem(
    body: RedeemRequest,
    identity: CurrentIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    identity.require_role("merchant_owner", "operation_user")
    key = _require_key(idempotency_key)
    repo = get_repo()

    def compute() -> StoredResponse:
        card = repo.get_card(str(body.cardId))
        if card is None:
            raise NotFound("card not found")
        identity.require_merchant(card.merchant_id)
        _require_active(card)
        location_id = _resolve_location(identity, card.merchant_id, body.locationId)
        if card.program_type == SEASONAL:
            # Applying a seasonal discount: only while the campaign is active, once.
            program = repo.get_program(card.program_id)
            if program is None:
                raise NotFound("program not found")
            require_active(program)
            if card.state.rewards_available < 1:
                raise DiscountAlreadyUsed("this discount has already been used")
        try:
            card.state = logic.redeem(card.state)
        except logic.RewardMechanicError as exc:
            raise Conflict(str(exc)) from exc
        txn = TxnRow(str(uuid.uuid4()), card.id, "redeem", 0, 0,
                     str(body.operationUserId), _now(), location_id)
        repo.commit(card, txn)
        _push_wallet_update(repo, card.id)
        return StoredResponse(200, _operation_result(card, txn))

    return run_idempotent(get_idempotency_store(), key=key, endpoint="redeem",
                          payload=body.model_dump(mode="json"), compute=compute).body


@app.post("/operations/validate-access")
def validate_access(
    body: ValidateAccessRequest,
    identity: CurrentIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    identity.require_role("merchant_owner", "operation_user")
    _require_key(idempotency_key)
    repo = get_repo()
    card = repo.get_card(str(body.cardId))
    if card is None:
        raise NotFound("card not found")
    identity.require_merchant(card.merchant_id)
    granted, reason = logic.validate_access(card.state)
    return AccessDecision.model_validate({
        "cardId": card.id, "granted": granted, "reason": reason,
        "validUntil": card.state.membership_active_until.isoformat()
        if card.state.membership_active_until else None,
    }).model_dump(by_alias=True, exclude_none=True)


def _redemption_model(r: dict) -> dict:
    out = {
        "id": r["id"], "customerName": r["customer_name"],
        "programId": r["program_id"], "reward": r["reward"],
        "redeemedAt": r["redeemed_at"],
    }
    if r.get("customer_phone") is not None:
        out["customerPhone"] = r["customer_phone"]
    if r.get("customer_email") is not None:
        out["customerEmail"] = r["customer_email"]
    if r.get("program") is not None:
        out["program"] = r["program"]
    return out


@app.get("/programs/{program_id}/redemptions")
def list_redemptions(program_id: str, identity: CurrentIdentity,
                     from_: Annotated[str | None, Query(alias="from")] = None,
                     to: str | None = None):
    identity.require_role("merchant_owner", "operation_user")
    repo = get_repo()
    program = repo.get_program(program_id)
    if program is None:
        raise NotFound("program not found")
    identity.require_merchant(program["merchant_id"])
    rows = repo.list_redemptions(program_id, from_, to)
    return [_redemption_model(r) for r in rows]


handler = lambda_handler(app)
