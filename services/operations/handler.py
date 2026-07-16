"""operations service — in-store accrue / redeem / validate / resolve.

Single-responsibility Lambda. Mutations are idempotent (offline-replayable, B5) and
enforce a per-window fraud guard. Balance changes are backend-authoritative; the wallet
push is queued asynchronously (never blocks the < 3s cashier op).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import (
    Conflict,
    NotFound,
    NotImplementedYet,
    RateLimited,
    ValidationFailed,
)
from conpass_common.idempotency import IdempotencyStore, StoredResponse, run_idempotent
from conpass_common.models import (
    AccessDecision,
    AccrueRequest,
    OperationResult,
    RedeemRequest,
    ScanResolveRequest,
    ValidateAccessRequest,
)
from fastapi import Header

from . import logic
from .repository import CardRow, OperationsRepository, SupabaseRepository, TxnRow

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
            },
            "active": card.state.active, "createdAt": _now().isoformat(),
        },
        "transaction": {
            "id": txn.id, "cardId": txn.card_id, "kind": txn.kind,
            "stampsDelta": txn.stamps_delta, "pointsDelta": txn.points_delta,
            "operationUserId": txn.operation_user_id, "createdAt": txn.created_at.isoformat(),
        },
        "walletUpdateQueued": True,
    }).model_dump(by_alias=True, exclude_none=True)


@app.post("/operations/resolve")
def resolve_scan(body: ScanResolveRequest, identity: CurrentIdentity):
    identity.require_role("merchant_owner", "operation_user")
    repo = get_repo()
    card = repo.get_card_by_token(body.code)
    if card is None:
        raise NotFound("card not found for code")
    identity.require_merchant(card.merchant_id)
    # Phase 3: hydrate the full Program (name/appearance/wallets) for the cashier UI.
    raise NotImplementedYet("Phase 3")


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
        rules = repo.get_rules(card.program_id)
        if rules is None:
            raise NotFound("program not found")
        if repo.recent_accrual_count(card.id, ACCRUAL_WINDOW_SECONDS) >= MAX_ACCRUALS_PER_WINDOW:
            raise RateLimited("too many accruals in the fraud window")
        try:
            outcome = logic.accrue(rules, card.state, stamps=body.stamps, amount=body.amount)
        except logic.RewardMechanicError as exc:
            raise ValidationFailed(str(exc)) from exc
        card.state = outcome.state
        txn = TxnRow(str(uuid.uuid4()), card.id, outcome.kind, outcome.stamps_delta,
                     outcome.points_delta, str(body.operationUserId), _now())
        repo.commit(card, txn)
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
        try:
            card.state = logic.redeem(card.state)
        except logic.RewardMechanicError as exc:
            raise Conflict(str(exc)) from exc
        txn = TxnRow(str(uuid.uuid4()), card.id, "redeem", 0, 0,
                     str(body.operationUserId), _now())
        repo.commit(card, txn)
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


@app.get("/programs/{program_id}/redemptions")
def list_redemptions(program_id: str, identity: CurrentIdentity,
                     from_: str | None = None, to: str | None = None):
    identity.require_role("merchant_owner", "operation_user")
    # Reads redemptions_view filtered by program + date range.
    raise NotImplementedYet("Phase 3")


handler = lambda_handler(app)
