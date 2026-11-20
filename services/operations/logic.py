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
    rewards_redeemed: int = 0     # total ever redeemed — accrual never touches this
    level: int = 1                # 1 + redemptions (loyalty cards); never drops (D21)


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


def redeem(state: CardState, *, raise_level: bool = False) -> CardState:
    """Spend one reward. `raise_level` (loyalty cards) also takes the level up by one."""
    if state.rewards_available < 1:
        raise RewardMechanicError("no reward available to redeem")
    return replace(state, rewards_available=state.rewards_available - 1,
                   rewards_redeemed=state.rewards_redeemed + 1,
                   level=state.level + 1 if raise_level else state.level)


class UndoError(ValueError):
    """The stored data cannot be inverted (the card changed in a way the undo can't see)."""


def unaccrue(rules: ProgramRules, state: CardState, *, stamps_delta: int,
             points_delta: int) -> CardState:
    """Exact inverse of `accrue` for the card's LATEST operation.

    accrue computed  total = held + delta;  earned = total // goal;  held' = total - earned*goal;
    rewards' = rewards + earned.  Given the post-accrual (held', rewards') and the stored
    delta, the pre-accrual held is  held' + earned*goal - delta.  `earned` is the smallest
    count that keeps that non-negative, i.e. the one that puts held back inside [0, goal) —
    unique for any card whose balance was normalised by earlier accruals. It can never
    exceed the rewards the card holds now (a redemption since would be a newer operation).
    """
    if rules.mechanic == "stamps":
        held, delta, goal = state.stamps, stamps_delta, rules.stamps_for_reward or 0
    elif rules.mechanic == "points":
        held, delta, goal = state.points, points_delta, rules.points_for_reward or 0
    else:
        raise UndoError("program mechanic does not accrue")
    # ceil((delta - held) / goal), never negative
    earned = max(0, -((held - delta) // goal)) if goal else 0
    before = held + earned * goal - delta
    if before < 0 or earned > state.rewards_available:
        raise UndoError("balance no longer matches the operation")
    new_rewards = state.rewards_available - earned
    if rules.mechanic == "stamps":
        return replace(state, stamps=before, rewards_available=new_rewards)
    return replace(state, points=before, rewards_available=new_rewards)


def unredeem(state: CardState, *, lowered_level: bool) -> CardState:
    """Inverse of `redeem`. `lowered_level` for loyalty cards (level never drops below 1)."""
    return replace(state, rewards_available=state.rewards_available + 1,
                   rewards_redeemed=max(0, state.rewards_redeemed - 1),
                   level=max(1, state.level - 1) if lowered_level else state.level)


def validate_access(state: CardState, *, today: date | None = None) -> tuple[bool, str]:
    today = today or date.today()
    if not state.active:
        return False, "membership inactive"
    if state.membership_active_until is None:
        return False, "no membership validity on card"
    if state.membership_active_until < today:
        return False, f"expired {state.membership_active_until.isoformat()}"
    return True, "access granted"
