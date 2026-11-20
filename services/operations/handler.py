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
from conpass_common.campaigns import (
    SEASONAL,
    campaign_fields,
    inactivity_fields,
    require_active,
    today_ec,
)
from conpass_common.cards import card_flags
from conpass_common.config import settings
from conpass_common.db import service_client
from conpass_common.errors import (
    Conflict,
    DiscountAlreadyUsed,
    Forbidden,
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
    RecentOperation,
    RedeemRequest,
    ScanResolveRequest,
    ValidateAccessRequest,
)
from conpass_common.notify.automations import AccrualEvent, after_accrual, after_redemption
from fastapi import Header, Query

from . import logic
from .logic import CardState
from .repository import (
    CardRow,
    DuplicateReversal,
    OperationsRepository,
    SupabaseRepository,
    TxnRow,
)

log = logging.getLogger(__name__)
# Card's enrollment link base (mirrors the programs service).
# Phase 18 review: the PWA (and its /enroll route) lives at the public app URL
# (console.conpass.cards); conpass.cards is the marketing site and answered 404.
ENROLL_BASE = f"{settings.public_app_url}/enroll"

app = create_app(service="operations")

# Fraud guard: at most N accruals per card per window (B5). Tunable per program later.
ACCRUAL_WINDOW_SECONDS = 60
MAX_ACCRUALS_PER_WINDOW = 5

# O3: the cashier can undo only while "Listo" shows (~10 s); 30 s covers network slack.
UNDO_WINDOW_SECONDS = 30
UNDOABLE_KINDS = ("accrue_stamps", "accrue_points", "redeem", "birthday_redeem")
LOYALTY_TYPES = ("loyalty_stamps", "loyalty_points")


def get_repo() -> OperationsRepository:
    return SupabaseRepository()


def get_idempotency_store() -> IdempotencyStore:
    return IdempotencyStore()


# Phase 18 (Opus review): transactions record the AUTHENTICATED caller, never the client's
# `operationUserId`. That field stays in the contract for compatibility but is ignored:
# trusting it let one cashier book (or later undo) operations under another's name.


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
            "active": card.state.active, "level": card.state.level,
            "paused": card.paused, "createdAt": _now().isoformat(),
        },
        "transaction": {
            "id": txn.id, "cardId": txn.card_id, "kind": txn.kind,
            "stampsDelta": txn.stamps_delta, "pointsDelta": txn.points_delta,
            "operationUserId": txn.operation_user_id, "locationId": txn.location_id,
            "reversesId": txn.reverses_id,
            "reversedAt": txn.reversed_at.isoformat() if txn.reversed_at else None,
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
        **card_flags(c),
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


def _reactivate(repo: OperationsRepository, card_id: str, row: dict | None = None) -> None:
    """A scan wakes a paused card (O2). Best-effort: it must never fail the scan."""
    try:
        if repo.reactivate_if_paused(card_id) and row is not None:
            row["paused_at"] = None
    except Exception:  # noqa: BLE001 - reactivation is retried by the next scan
        log.warning("reactivate failed for card %s", card_id, exc_info=True)


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
    # A scan counts as use (O2): a paused card wakes up with its balance intact.
    _reactivate(repo, card["id"], card)
    out = {"card": _card_model(card, program), "program": _program_model(program)}
    reward = repo.get_birthday_reward(card["id"], today_ec()) if card.get("active", True) else None
    if reward is not None:
        out["birthdayReward"] = {"id": reward["id"], "benefit": reward.get("benefit") or "",
                                 "validUntil": str(reward["valid_until"])[:10]}
    return CardResolution.model_validate(out).model_dump(by_alias=True, exclude_none=True)


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
        _reactivate(repo, card.id)
        card.paused = False
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
                     outcome.points_delta, identity.user_id, _now(), location_id)
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
        _reactivate(repo, card.id)
        card.paused = False
        location_id = _resolve_location(identity, card.merchant_id, body.locationId)
        if body.birthdayRewardId is not None:
            return _redeem_birthday(repo, card, body, location_id, identity.user_id)
        loyalty = card.program_type in LOYALTY_TYPES
        if card.program_type == SEASONAL:
            # Applying a seasonal discount: only while the campaign is active, once.
            program = repo.get_program(card.program_id)
            if program is None:
                raise NotFound("program not found")
            require_active(program)
            if card.state.rewards_available < 1:
                raise DiscountAlreadyUsed("this discount has already been used")
        try:
            # Level goes up in the SAME card update as the balance (D21 decision 12).
            card.state = logic.redeem(card.state, raise_level=loyalty)
        except logic.RewardMechanicError as exc:
            raise Conflict(str(exc)) from exc
        txn = TxnRow(str(uuid.uuid4()), card.id, "redeem", 0, 0,
                     identity.user_id, _now(), location_id)
        repo.commit(card, txn)
        _push_wallet_update(repo, card.id)
        if loyalty:
            _notify_level_up(card)
        return StoredResponse(200, _operation_result(card, txn))

    return run_idempotent(get_idempotency_store(), key=key, endpoint="redeem",
                          payload=body.model_dump(mode="json"), compute=compute).body


def _notify_level_up(card: CardRow) -> None:
    """After a loyalty redemption commits: the level-up automation. Best-effort."""
    try:
        after_redemption(service_client(), card_id=card.id, merchant_id=card.merchant_id,
                         program_id=card.program_id, new_level=card.state.level)
    except Exception:  # noqa: BLE001 - notifications never fail a redemption
        log.warning("level-up automation failed for card %s", card.id, exc_info=True)


def _redeem_birthday(repo: OperationsRepository, card: CardRow, body: RedeemRequest,
                     location_id: str | None, user_id: str) -> StoredResponse:
    """Redeem the card's birthday reward instead of the campaign reward: no balance or
    level change, one `birthday_redeem` transaction."""
    reward = repo.get_birthday_card(str(body.birthdayRewardId))
    invalid = ValidationFailed("birthday reward is not valid for this card",
                               details={"reason": "birthday_reward_invalid"})
    if (reward is None or reward.get("card_id") != card.id
            or reward.get("status") != "issued" or reward.get("redeemed_at")
            or not reward.get("valid_until")
            or str(reward["valid_until"])[:10] < today_ec().isoformat()):
        raise invalid
    now = _now()
    txn = TxnRow(str(uuid.uuid4()), card.id, "birthday_redeem", 0, 0,
                 user_id, now, location_id)
    repo.set_birthday_state(reward["id"], "redeemed", now)
    repo.commit(card, txn)
    _push_wallet_update(repo, card.id)
    return StoredResponse(200, _operation_result(card, txn))


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


def _holder_label(full_name: str | None, holder_name: str | None) -> str:
    """"María T." from a full name (first name + last initial), else the card's holder name,
    else "Cliente"."""
    if full_name and full_name.split():
        parts = full_name.split()
        return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0].upper()}."
    return holder_name.strip() if holder_name and holder_name.strip() else "Cliente"


def _undo_block(repo: OperationsRepository, txn: TxnRow, user_id: str,
                now: datetime) -> tuple[int, str, str] | None:
    """Why `txn` can't be undone by `user_id` right now: (status, code, message) — in the
    rule order of O3 — or None when it can."""
    if txn.operation_user_id != user_id:
        return 403, "forbidden", "only the user who made the operation can undo it"
    if txn.kind not in UNDOABLE_KINDS:
        return 409, "not_undoable", "this operation cannot be undone"
    if txn.reversed_at is not None:
        return 409, "already_reversed", "this operation was already undone"
    if (now - txn.created_at).total_seconds() > UNDO_WINDOW_SECONDS:
        return 409, "undo_window_closed", "the undo window has closed"
    if repo.has_newer_effective(txn):
        return 409, "not_latest", "a newer operation exists on this card"
    return None


@app.get("/operations/recent")
def recent_operations(
    identity: CurrentIdentity,
    locationId: uuid.UUID | None = None,  # noqa: N803 - the contract's query name
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
):
    identity.require_role("merchant_owner", "operation_user")
    if not identity.merchant_id:
        raise Forbidden("requires a merchant")
    repo = get_repo()
    mine = None if "merchant_owner" in identity.roles else identity.user_id
    rows = repo.list_recent(identity.merchant_id, operation_user_id=mine,
                            location_id=str(locationId) if locationId else None, limit=limit)
    now = _now()
    out = []
    for r in rows:
        t: TxnRow = r["txn"]
        # Cheap age test first: only a just-made operation can still be undoable.
        undoable = ((now - t.created_at).total_seconds() <= UNDO_WINDOW_SECONDS
                    and _undo_block(repo, t, identity.user_id, now) is None)
        item = {
            "id": t.id, "cardId": t.card_id, "kind": t.kind,
            "holderName": _holder_label(r["full_name"], r["holder_name"]),
            "stampsDelta": t.stamps_delta, "pointsDelta": t.points_delta,
            "locationId": t.location_id, "reversed": t.reversed_at is not None,
            "undoable": undoable, "createdAt": t.created_at.isoformat(),
        }
        if r["program_name"]:
            item["programName"] = r["program_name"]
        out.append(RecentOperation.model_validate(item).model_dump(
            by_alias=True, exclude_none=True, mode="json"))
    return out


@app.post("/operations/{transaction_id}/undo")
def undo_operation(
    transaction_id: uuid.UUID,
    identity: CurrentIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """O3 — reverse the caller's latest operation while "Listo" still shows."""
    identity.require_role("merchant_owner", "operation_user")
    key = _require_key(idempotency_key)
    repo = get_repo()
    txn_id = str(transaction_id)

    def compute() -> StoredResponse:
        txn = repo.get_transaction(txn_id)
        card = repo.get_card(txn.card_id) if txn else None
        # 404 for a missing or foreign operation alike: no probing across merchants.
        if txn is None or card is None or card.merchant_id != identity.merchant_id:
            raise NotFound("operation not found")
        now = _now()
        block = _undo_block(repo, txn, identity.user_id, now)
        if block is not None:
            status, code, message = block
            if status == 403:
                raise Forbidden(message)
            raise Conflict(message, details={"reason": code})
        rules = repo.get_rules(card.program_id)
        loyalty = card.program_type in LOYALTY_TYPES
        birthday = None
        try:
            if txn.kind in ("accrue_stamps", "accrue_points"):
                if rules is None:
                    raise NotFound("program not found")
                card.state = logic.unaccrue(rules, card.state, stamps_delta=txn.stamps_delta,
                                            points_delta=txn.points_delta)
            elif txn.kind == "redeem":
                card.state = logic.unredeem(card.state, lowered_level=loyalty)
            else:
                birthday = _birthday_for_txn(repo, txn)
        except logic.UndoError as exc:
            raise Conflict(str(exc), details={"reason": "not_undoable"}) from exc
        reversal = TxnRow(str(uuid.uuid4()), card.id, "reversal", -txn.stamps_delta,
                          -txn.points_delta, identity.user_id, now, txn.location_id,
                          reverses_id=txn.id)
        try:
            repo.commit_undo(card, reversal, txn, now)
        except DuplicateReversal as exc:
            raise Conflict("this operation was already undone",
                           details={"reason": "already_reversed"}) from exc
        if birthday is not None:
            repo.set_birthday_state(birthday["id"], "issued", None)
        _push_wallet_update(repo, card.id)
        return StoredResponse(200, _operation_result(card, reversal))

    return run_idempotent(get_idempotency_store(), key=key, endpoint="undo",
                          payload={"transactionId": txn_id}, compute=compute).body


def _birthday_for_txn(repo: OperationsRepository, txn: TxnRow) -> dict:
    """The birthday reward a `birthday_redeem` spent: the card's reward redeemed at that
    moment (redeemed_at within a couple of seconds of the transaction)."""
    best = repo.find_redeemed_birthday(txn.card_id, txn.created_at)
    if best is None:
        raise logic.UndoError("birthday reward not found")
    return best


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
