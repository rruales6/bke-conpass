"""Pure accrual/redeem/validate logic — backend is the authority for balances (B1).

No I/O here: takes state + program rules, returns the new state and a transaction
description. This is the highest-risk logic, so it is pure and unit-tested offline.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date


@dataclass(frozen=True)
class ProgramRules:
    program_type: str            # loyalty_stamps | loyalty_points | membership_pass
    mechanic: str | None         # stamps | points
    stamps_for_reward: int | None
    points_for_reward: int | None
    points_per_dollar: float | None


@dataclass(frozen=True)
class CardState:
    stamps: int
    points: int
    rewards_available: int
    active: bool
    membership_active_until: date | None = None


@dataclass(frozen=True)
class AccrualOutcome:
    state: CardState
    kind: str                    # accrue_stamps | accrue_points
    stamps_delta: int
    points_delta: int
    rewards_earned: int


class RewardMechanicError(ValueError):
    """Raised when the request doesn't match the program's mechanic."""


def accrue(rules: ProgramRules, state: CardState, *,
           stamps: int | None = None, amount: float | None = None) -> AccrualOutcome:
    if not state.active:
        raise RewardMechanicError("card is inactive")

    if rules.mechanic == "stamps":
        if stamps is None or stamps < 1:
            raise RewardMechanicError("stamps mechanic requires a positive `stamps`")
        threshold = rules.stamps_for_reward or 0
        total = state.stamps + stamps
        earned = total // threshold if threshold else 0
        remaining = total - earned * threshold if threshold else total
        new = replace(state, stamps=remaining,
                     rewards_available=state.rewards_available + earned)
        return AccrualOutcome(new, "accrue_stamps", stamps, 0, earned)

    if rules.mechanic == "points":
        if amount is None or amount < 0:
            raise RewardMechanicError("points mechanic requires a non-negative `amount`")
        gained = int(amount * (rules.points_per_dollar or 0))
        threshold = rules.points_for_reward or 0
        total = state.points + gained
        earned = total // threshold if threshold else 0
        remaining = total - earned * threshold if threshold else total
        new = replace(state, points=remaining,
                     rewards_available=state.rewards_available + earned)
        return AccrualOutcome(new, "accrue_points", 0, gained, earned)

    raise RewardMechanicError(f"program mechanic '{rules.mechanic}' does not accrue")


def redeem(state: CardState) -> CardState:
    if state.rewards_available < 1:
        raise RewardMechanicError("no reward available to redeem")
    return replace(state, rewards_available=state.rewards_available - 1)


def validate_access(state: CardState, *, today: date | None = None) -> tuple[bool, str]:
    today = today or date.today()
    if not state.active:
        return False, "membership inactive"
    if state.membership_active_until is None:
        return False, "no membership validity on card"
    if state.membership_active_until < today:
        return False, f"expired {state.membership_active_until.isoformat()}"
    return True, "access granted"
