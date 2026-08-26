"""Unit tests for the backend-authoritative accrual logic (pure, no I/O)."""
from datetime import date, timedelta

import pytest

from services.operations import logic
from services.operations.logic import CardState, ProgramRules, RewardMechanicError

STAMPS = ProgramRules("loyalty_stamps", "stamps", stamps_for_reward=8,
                      points_for_reward=None, points_per_dollar=None)
POINTS = ProgramRules("loyalty_points", "points", stamps_for_reward=None,
                      points_for_reward=100, points_per_dollar=1.0)


def fresh(**kw) -> CardState:
    base = dict(stamps=0, points=0, rewards_available=0, active=True, rewards_redeemed=0)
    base.update(kw)
    return CardState(**base)


def test_stamps_accrue_below_threshold():
    out = logic.accrue(STAMPS, fresh(stamps=5), stamps=2)
    assert out.state.stamps == 7
    assert out.rewards_earned == 0
    assert out.kind == "accrue_stamps"


def test_accrual_never_touches_rewards_redeemed():
    # rewards_redeemed only ever moves in redeem(); accrual — stamps or points,
    # reward-earning or not — must leave it exactly as it found it.
    out = logic.accrue(STAMPS, fresh(stamps=7, rewards_redeemed=3), stamps=3)  # earns 1
    assert out.rewards_earned == 1
    assert out.state.rewards_redeemed == 3
    out2 = logic.accrue(POINTS, fresh(points=10, rewards_redeemed=5), amount=1)  # no reward
    assert out2.rewards_earned == 0
    assert out2.state.rewards_redeemed == 5


def test_stamps_reaching_threshold_earns_reward_and_rolls_over():
    out = logic.accrue(STAMPS, fresh(stamps=7), stamps=3)  # 10 total, threshold 8
    assert out.rewards_earned == 1
    assert out.state.rewards_available == 1
    assert out.state.stamps == 2  # 10 - 8 carried over


def test_stamps_multiple_rewards_in_one_accrual():
    out = logic.accrue(STAMPS, fresh(stamps=0), stamps=17)  # 2 rewards, 1 left
    assert out.rewards_earned == 2
    assert out.state.stamps == 1


def test_points_accrue_by_amount():
    out = logic.accrue(POINTS, fresh(points=60), amount=60)  # +60 → 120 → 1 reward, 20 left
    assert out.points_delta == 60
    assert out.rewards_earned == 1
    assert out.state.points == 20


def test_points_truncates_fractional():
    rules = ProgramRules("loyalty_points", "points", None, 100, 1.5)
    out = logic.accrue(rules, fresh(), amount=3.4)  # 3.4 * 1.5 = 5.1 → 5
    assert out.points_delta == 5


def test_wrong_mechanic_raises():
    with pytest.raises(RewardMechanicError):
        logic.accrue(STAMPS, fresh(), amount=10)      # stamps program, gave amount
    with pytest.raises(RewardMechanicError):
        logic.accrue(POINTS, fresh(), stamps=1)        # points program, gave stamps


def test_inactive_card_cannot_accrue():
    with pytest.raises(RewardMechanicError):
        logic.accrue(STAMPS, fresh(active=False), stamps=1)


def test_redeem_decrements_and_guards_empty():
    redeemed = logic.redeem(fresh(rewards_available=2, rewards_redeemed=4))
    assert redeemed.rewards_available == 1
    assert redeemed.rewards_redeemed == 5   # ever-redeemed counter goes up, not just down
    with pytest.raises(RewardMechanicError):
        logic.redeem(fresh(rewards_available=0))


def test_validate_access():
    ok, _ = logic.validate_access(fresh(membership_active_until=date.today() + timedelta(days=1)))
    assert ok
    expired, reason = logic.validate_access(
        fresh(membership_active_until=date.today() - timedelta(days=1)))
    assert not expired and "expired" in reason
    none, _ = logic.validate_access(fresh())
    assert not none
