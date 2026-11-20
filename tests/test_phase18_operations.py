"""Phase 18 (D21) — operations: undo (O3), recent, birthday rewards, level, pause.

In-memory repository + fake idempotency store; no DB, no network, no wallet.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.campaigns import today_ec
from conpass_common.idempotency import InMemoryIdempotencyStore
from fastapi.testclient import TestClient

from services.operations import handler as h
from services.operations import logic
from services.operations.logic import CardState, ProgramRules
from services.operations.repository import (
    CardRow,
    DuplicateReversal,
    InMemoryRepository,
    SupabaseRepository,
    TxnRow,
)
from tests.fakedb import FakeDB

M1 = "22222222-2222-2222-2222-222222222222"
M2 = "22222222-2222-2222-2222-222222222223"
P1 = "33333333-3333-3333-3333-333333333333"
CARD1 = "44444444-4444-4444-4444-444444444444"
CARD2 = "44444444-4444-4444-4444-444444444445"
OP1 = "55555555-5555-5555-5555-555555555555"
OP2 = "55555555-5555-5555-5555-555555555556"
OWNER = "55555555-5555-5555-5555-555555555557"
LOC1 = "66666666-6666-6666-6666-666666666666"
LOC2 = "66666666-6666-6666-6666-666666666667"
T0 = datetime(2026, 11, 19, 15, 0, 0, tzinfo=UTC)


def _key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


class Env:
    def __init__(self, monkeypatch, *, ptype="loyalty_stamps", state=None, who=OP1,
                 roles=("operation_user",), merchant=M1):
        self.repo = InMemoryRepository()
        self.clock = [T0]
        self.pushes: list[str] = []
        self.levelups: list[dict] = []
        mechanic = "points" if ptype == "loyalty_points" else "stamps"
        self.repo.rules[P1] = ProgramRules(
            ptype, mechanic, 8 if mechanic == "stamps" else None,
            100 if mechanic == "points" else None, 1.0 if mechanic == "points" else None)
        self.repo.programs[P1] = {
            "id": P1, "merchant_id": M1, "type": ptype, "name": "Café", "mechanic": mechanic,
            "stamps_for_reward": 8, "points_for_reward": 100, "active": True,
            "created_at": T0.isoformat()}
        self.put_card(CARD1, state or CardState(7, 0, 0, True), ptype=ptype)
        self.store = InMemoryIdempotencyStore()
        monkeypatch.setattr(h, "get_repo", lambda: self.repo)
        monkeypatch.setattr(h, "get_idempotency_store", lambda: self.store)
        monkeypatch.setattr(h, "_now", lambda: self.clock[0])
        monkeypatch.setattr(h, "service_client", lambda: object())
        monkeypatch.setattr(h, "_push_wallet_update",
                            lambda repo, card_id: self.pushes.append(card_id))
        monkeypatch.setattr(h, "after_redemption", lambda *_a, **kw: self.levelups.append(kw))
        self.as_user(who, roles, merchant)
        self.monkeypatch = monkeypatch
        self.client = TestClient(h.app)

    def as_user(self, who, roles=("operation_user",), merchant=M1):
        self.monkeypatch_dep(lambda: Identity(
            user_id=who, email="c@x.co", roles=list(roles), merchant_id=merchant))

    def monkeypatch_dep(self, fn):
        h.app.dependency_overrides[require_identity] = fn

    def put_card(self, card_id, state, *, ptype="loyalty_stamps", merchant=M1, row_extra=None):
        self.repo.cards[card_id] = CardRow(card_id, P1, merchant, ptype, state)
        self.repo.card_rows[card_id] = {
            "id": card_id, "program_id": P1, "merchant_id": merchant, "type": ptype,
            "opaque_token": f"tok-{card_id[-1]}", "stamps": state.stamps,
            "points": state.points, "rewards_available": state.rewards_available,
            "rewards_redeemed": state.rewards_redeemed, "level": state.level,
            "active": state.active, "created_at": T0.isoformat(), **(row_extra or {})}

    def txn(self, kind="accrue_stamps", *, card=CARD1, who=OP1, age=0, stamps=0, points=0,
            reversed_at=None, reverses_id=None, location=None) -> TxnRow:
        t = TxnRow(str(uuid.uuid4()), card, kind, stamps, points, who,
                   self.clock[0] - timedelta(seconds=age), location, reverses_id, reversed_at)
        self.repo.txns.append(t)
        return t

    def accrue(self, stamps=1, card=CARD1, who=OP1):
        return self.client.post("/operations/accrue", headers=_key(), json={
            "cardId": card, "operationUserId": who, "stamps": stamps})

    def redeem(self, card=CARD1, who=OP1, **extra):
        return self.client.post("/operations/redeem", headers=_key(), json={
            "cardId": card, "operationUserId": who, **extra})

    def undo(self, txn_id, headers=None):
        return self.client.post(f"/operations/{txn_id}/undo", headers=headers or _key())


@pytest.fixture
def env(monkeypatch):
    e = Env(monkeypatch)
    yield e
    h.app.dependency_overrides.clear()


def _reason(resp):
    return resp.json()["details"]["reason"]


# --------------------------------------------------------------------------- #
# logic.unaccrue — the inverse of accrue, every rollover case
# --------------------------------------------------------------------------- #
def _stamps_rules(goal=8):
    return ProgramRules("loyalty_stamps", "stamps", goal, None, None)


def _points_rules(goal=100, ppd=1.0):
    return ProgramRules("loyalty_points", "points", None, goal, ppd)


def test_unaccrue_no_crossing():
    s = CardState(3, 0, 0, True)
    out = logic.accrue(_stamps_rules(), s, stamps=2)
    assert out.state.stamps == 5
    assert logic.unaccrue(_stamps_rules(), out.state, stamps_delta=2, points_delta=0) == s


def test_unaccrue_crossing_once():
    s = CardState(7, 0, 0, True)
    out = logic.accrue(_stamps_rules(), s, stamps=3)       # 10 -> 2 left, +1 reward
    assert (out.state.stamps, out.state.rewards_available) == (2, 1)
    assert logic.unaccrue(_stamps_rules(), out.state, stamps_delta=3, points_delta=0) == s


def test_unaccrue_crossing_exactly_at_goal():
    s = CardState(7, 0, 0, True)
    out = logic.accrue(_stamps_rules(), s, stamps=1)       # 8 -> 0 left, +1 reward
    assert (out.state.stamps, out.state.rewards_available) == (0, 1)
    back = logic.unaccrue(_stamps_rules(), out.state, stamps_delta=1, points_delta=0)
    assert back == s


def test_unaccrue_crossing_twice_keeps_older_rewards():
    s = CardState(5, 0, 2, True)
    out = logic.accrue(_stamps_rules(), s, stamps=12)      # 17 -> 1 left, +2 rewards
    assert (out.state.stamps, out.state.rewards_available) == (1, 4)
    assert logic.unaccrue(_stamps_rules(), out.state, stamps_delta=12, points_delta=0) == s


def test_unaccrue_points_with_leftover():
    s = CardState(0, 90, 0, True)
    out = logic.accrue(_points_rules(), s, amount=235)     # 325 -> 25 left, +3 rewards
    assert (out.state.points, out.state.rewards_available) == (25, 3)
    assert logic.unaccrue(_points_rules(), out.state, stamps_delta=0, points_delta=235) == s


def test_unaccrue_points_exact_multiple_and_zero_gain():
    s = CardState(0, 40, 1, True)
    out = logic.accrue(_points_rules(), s, amount=60)      # exactly 100 -> 0 left
    assert (out.state.points, out.state.rewards_available) == (0, 2)
    assert logic.unaccrue(_points_rules(), out.state, stamps_delta=0, points_delta=60) == s
    zero = logic.accrue(_points_rules(), s, amount=0)
    assert logic.unaccrue(_points_rules(), zero.state, stamps_delta=0, points_delta=0) == s


def test_unaccrue_is_exact_inverse_for_every_balance_and_delta():
    rules = _stamps_rules(8)
    for before in range(8):
        for delta in range(1, 40):
            s = CardState(before, 0, 3, True)
            out = logic.accrue(rules, s, stamps=delta)
            assert logic.unaccrue(rules, out.state, stamps_delta=delta, points_delta=0) == s


def test_unaccrue_refuses_data_that_no_longer_matches():
    # 1 reward claimed to come from a crossing, but the card holds none -> cannot invert
    state = CardState(0, 0, 0, True)
    with pytest.raises(logic.UndoError):
        logic.unaccrue(_stamps_rules(), state, stamps_delta=3, points_delta=0)


def test_unredeem_restores_reward_and_level():
    s = CardState(0, 0, 1, True, rewards_redeemed=0, level=1)
    spent = logic.redeem(s, raise_level=True)
    assert (spent.rewards_available, spent.rewards_redeemed, spent.level) == (0, 1, 2)
    assert logic.unredeem(spent, lowered_level=True) == s
    assert logic.unredeem(CardState(0, 0, 0, True, 0, level=1), lowered_level=True).level == 1


# --------------------------------------------------------------------------- #
# undo endpoint
# --------------------------------------------------------------------------- #
def test_undo_accrual_crossing_restores_balance_and_writes_reversal(env):
    first = env.accrue()                                    # 7 -> 0 stamps, +1 reward
    assert first.json()["card"]["balance"]["rewardsAvailable"] == 1
    orig_id = first.json()["transaction"]["id"]
    r = env.undo(orig_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["card"]["balance"]["stamps"] == 7
    assert body["card"]["balance"]["rewardsAvailable"] == 0
    assert body["transaction"]["kind"] == "reversal"
    assert body["transaction"]["reversesId"] == orig_id
    assert body["transaction"]["stampsDelta"] == -1
    assert body["transaction"]["operationUserId"] == OP1
    stored = env.repo.cards[CARD1].state
    assert (stored.stamps, stored.rewards_available) == (7, 0)
    assert env.repo.get_transaction(orig_id).reversed_at == T0
    assert env.pushes[-1] == CARD1                          # wallet re-synced


def test_undo_points_accrual(monkeypatch):
    e = Env(monkeypatch, ptype="loyalty_points", state=CardState(0, 90, 0, True))
    r = e.client.post("/operations/accrue", headers=_key(), json={
        "cardId": CARD1, "operationUserId": OP1, "amount": 235})
    assert r.json()["card"]["balance"]["points"] == 25
    u = e.undo(r.json()["transaction"]["id"])
    assert u.json()["card"]["balance"]["points"] == 90
    assert u.json()["card"]["balance"]["rewardsAvailable"] == 0
    assert u.json()["transaction"]["pointsDelta"] == -235
    h.app.dependency_overrides.clear()


def test_undo_redeem_gives_reward_and_level_back(monkeypatch):
    e = Env(monkeypatch, state=CardState(2, 0, 1, True, rewards_redeemed=0, level=1))
    red = e.redeem()
    assert red.json()["card"]["level"] == 2
    assert red.json()["card"]["balance"]["rewardsRedeemed"] == 1
    assert e.repo.cards[CARD1].state.level == 2             # persisted in the same commit
    u = e.undo(red.json()["transaction"]["id"])
    assert u.status_code == 200
    card = u.json()["card"]
    assert card["level"] == 1
    assert (card["balance"]["rewardsAvailable"], card["balance"]["rewardsRedeemed"]) == (1, 0)
    h.app.dependency_overrides.clear()


def test_undo_redeem_never_drops_level_below_one(monkeypatch):
    e = Env(monkeypatch, state=CardState(0, 0, 0, True, rewards_redeemed=1, level=1))
    t = e.txn("redeem")
    u = e.undo(t.id)
    assert u.json()["card"]["level"] == 1
    h.app.dependency_overrides.clear()


def test_undo_seasonal_redeem_leaves_level_alone(monkeypatch):
    e = Env(monkeypatch, ptype="seasonal_discount", state=CardState(0, 0, 1, True))
    e.repo.programs[P1].update({"valid_from": "2020-01-01", "valid_until": "2099-01-01"})
    red = e.redeem()
    assert red.status_code == 200, red.text
    assert red.json()["card"]["level"] == 1
    assert e.levelups == []
    u = e.undo(red.json()["transaction"]["id"])
    assert u.json()["card"]["level"] == 1
    assert u.json()["card"]["balance"]["rewardsAvailable"] == 1
    h.app.dependency_overrides.clear()


def test_undo_unknown_or_foreign_transaction_is_404(env):
    assert env.undo(str(uuid.uuid4())).status_code == 404
    env.put_card(CARD2, CardState(1, 0, 0, True), merchant=M2)
    t = env.txn(card=CARD2)
    assert env.undo(t.id).status_code == 404


def test_undo_by_another_user_is_403(env):
    t = env.txn(who=OP2)
    r = env.undo(t.id)
    assert r.status_code == 403
    assert env.repo.get_transaction(t.id).reversed_at is None


def test_undo_owner_can_only_undo_their_own(env):
    env.as_user(OWNER, ("merchant_owner",))
    assert env.undo(env.txn(who=OP1).id).status_code == 403
    mine = env.txn(who=OWNER, stamps=1)
    env.repo.cards[CARD1].state = CardState(2, 0, 0, True)
    assert env.undo(mine.id).status_code == 200


@pytest.mark.parametrize("kind", ["validate_access", "birthday_issue", "reversal"])
def test_undo_other_kinds_are_not_undoable(env, kind):
    r = env.undo(env.txn(kind).id)
    assert r.status_code == 409 and _reason(r) == "not_undoable"


def test_undo_twice_is_already_reversed(env):
    t = env.txn(stamps=1)
    env.repo.cards[CARD1].state = CardState(2, 0, 0, True)
    assert env.undo(t.id).status_code == 200
    again = env.undo(t.id)                                  # a new key: a real second attempt
    assert again.status_code == 409 and _reason(again) == "already_reversed"
    assert sum(1 for x in env.repo.txns if x.reverses_id == t.id) == 1


def test_undo_unique_violation_maps_to_already_reversed(env, monkeypatch):
    t = env.txn(stamps=1)
    env.repo.cards[CARD1].state = CardState(2, 0, 0, True)

    def boom(*_a, **_k):
        raise DuplicateReversal(t.id)
    monkeypatch.setattr(env.repo, "commit_undo", boom)
    r = env.undo(t.id)
    assert r.status_code == 409 and _reason(r) == "already_reversed"


def test_undo_window_closes_after_30_seconds(env):
    ok = env.txn(stamps=1, age=30)
    env.repo.cards[CARD1].state = CardState(2, 0, 0, True)
    assert env.undo(ok.id).status_code == 200               # 30 s on the dot still counts
    env.put_card(CARD2, CardState(2, 0, 0, True))
    late = env.txn(card=CARD2, stamps=1, age=31)
    r = env.undo(late.id)
    assert r.status_code == 409 and _reason(r) == "undo_window_closed"


def test_undo_only_the_latest_operation(env):
    old = env.txn(stamps=1, age=10)
    env.txn(stamps=1, age=2)
    r = env.undo(old.id)
    assert r.status_code == 409 and _reason(r) == "not_latest"


def test_undo_ignores_newer_reversals_and_reversed_operations(env):
    base = env.txn(stamps=1, age=10)
    undone = env.txn(stamps=1, age=5, reversed_at=T0)       # newer but already undone
    env.txn("reversal", age=4, stamps=-1, reverses_id=undone.id)
    env.repo.cards[CARD1].state = CardState(2, 0, 0, True)
    assert env.undo(base.id).status_code == 200


def test_undo_rule_order_other_user_before_window(env):
    t = env.txn(who=OP2, age=120)
    assert env.undo(t.id).status_code == 403                # ownership is judged first


def test_undo_needs_idempotency_key(env):
    t = env.txn(stamps=1)
    r = env.client.post(f"/operations/{t.id}/undo")
    assert r.status_code == 422


def test_undo_replay_returns_same_response_once(env):
    t = env.txn(stamps=1)
    env.repo.cards[CARD1].state = CardState(2, 0, 0, True)
    key = _key()
    a = env.undo(t.id, key)
    b = env.undo(t.id, key)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert sum(1 for x in env.repo.txns if x.kind == "reversal") == 1
    assert env.repo.cards[CARD1].state.stamps == 1          # not undone twice


def test_undo_demo_style_merchant_is_the_same_path(env):
    # Demo merchants have no special casing: the same rules and effects apply.
    r = env.accrue(stamps=2)
    assert env.undo(r.json()["transaction"]["id"]).status_code == 200


# --------------------------------------------------------------------------- #
# redeem: level + automation
# --------------------------------------------------------------------------- #
def test_redeem_raises_persisted_level_and_calls_after_redemption(monkeypatch):
    e = Env(monkeypatch, state=CardState(0, 0, 1, True, level=3, rewards_redeemed=2))
    r = e.redeem()
    assert r.status_code == 200
    assert e.repo.cards[CARD1].state.level == 4
    assert e.levelups == [{"card_id": CARD1, "merchant_id": M1, "program_id": P1,
                           "new_level": 4}]
    h.app.dependency_overrides.clear()


def test_redeem_survives_a_failing_level_up_automation(monkeypatch):
    e = Env(monkeypatch, state=CardState(0, 0, 1, True))

    def boom(*_a, **_k):
        raise RuntimeError("automation down")
    monkeypatch.setattr(h, "after_redemption", boom)
    r = e.redeem()
    assert r.status_code == 200 and r.json()["card"]["level"] == 2
    h.app.dependency_overrides.clear()


def test_redeem_replay_does_not_raise_level_twice(monkeypatch):
    e = Env(monkeypatch, state=CardState(0, 0, 2, True))
    key = _key()
    body = {"cardId": CARD1, "operationUserId": OP1}
    e.client.post("/operations/redeem", headers=key, json=body)
    e.client.post("/operations/redeem", headers=key, json=body)
    assert e.repo.cards[CARD1].state.level == 2
    assert len(e.levelups) == 1
    h.app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# birthday rewards
# --------------------------------------------------------------------------- #
def _birthday(env, *, card=CARD1, status="issued", redeemed_at=None, days=3):
    bid = str(uuid.uuid4())
    env.repo.birthday_cards[bid] = {
        "id": bid, "card_id": card, "status": status, "redeemed_at": redeemed_at,
        "benefit": "Un postre de la casa",
        "valid_until": (today_ec() + timedelta(days=days)).isoformat()}
    return bid


def _resolve(env, card=CARD1):
    return env.client.post("/operations/resolve", json={"code": f"tok-{card[-1]}"})


def test_resolve_includes_issued_birthday_reward(env):
    bid = _birthday(env)
    body = _resolve(env).json()
    assert body["birthdayReward"] == {
        "id": bid, "benefit": "Un postre de la casa",
        "validUntil": (today_ec() + timedelta(days=3)).isoformat()}
    assert body["card"]["level"] == 1 and body["card"]["paused"] is False


@pytest.mark.parametrize("kw", [
    {"status": "redeemed"}, {"status": "pending"}, {"status": "expired"},
    {"redeemed_at": T0.isoformat()}, {"days": -1}, {"card": CARD2}])
def test_resolve_omits_birthday_reward_that_is_not_usable(env, kw):
    env.put_card(CARD2, CardState(1, 0, 0, True))
    _birthday(env, **kw)
    assert "birthdayReward" not in _resolve(env).json()


def test_resolve_birthday_valid_through_today(env):
    _birthday(env, days=0)
    assert "birthdayReward" in _resolve(env).json()


def test_redeem_birthday_reward_changes_no_balance(env):
    bid = _birthday(env)
    before = env.repo.cards[CARD1].state
    r = env.redeem(birthdayRewardId=bid)
    assert r.status_code == 200, r.text
    assert r.json()["transaction"]["kind"] == "birthday_redeem"
    assert env.repo.cards[CARD1].state == before
    assert env.repo.birthday_cards[bid]["status"] == "redeemed"
    assert env.repo.birthday_cards[bid]["redeemed_at"] == T0.isoformat()
    assert env.pushes == [CARD1]
    assert env.levelups == []
    assert "birthdayReward" not in _resolve(env).json()


def test_redeem_birthday_works_with_no_loyalty_reward_available(env):
    bid = _birthday(env)
    assert env.repo.cards[CARD1].state.rewards_available == 0
    assert env.redeem(birthdayRewardId=bid).status_code == 200


@pytest.mark.parametrize("kw", [
    {"status": "redeemed"}, {"redeemed_at": T0.isoformat()}, {"status": "pending"},
    {"days": -1}, {"card": CARD2}])
def test_redeem_birthday_invalid_is_422(env, kw):
    env.put_card(CARD2, CardState(1, 0, 0, True))
    bid = _birthday(env, **kw)
    r = env.redeem(birthdayRewardId=bid)
    assert r.status_code == 422 and _reason(r) == "birthday_reward_invalid"
    assert env.repo.txns == []


def test_redeem_unknown_birthday_id_is_422(env):
    r = env.redeem(birthdayRewardId=str(uuid.uuid4()))
    assert r.status_code == 422 and _reason(r) == "birthday_reward_invalid"


def test_birthday_cannot_be_redeemed_twice(env):
    bid = _birthday(env)
    assert env.redeem(birthdayRewardId=bid).status_code == 200
    again = env.redeem(birthdayRewardId=bid)
    assert again.status_code == 422 and _reason(again) == "birthday_reward_invalid"


def test_undo_birthday_redeem_reissues_the_reward(env):
    bid = _birthday(env)
    red = env.redeem(birthdayRewardId=bid)
    u = env.undo(red.json()["transaction"]["id"])
    assert u.status_code == 200, u.text
    assert env.repo.birthday_cards[bid]["status"] == "issued"
    assert env.repo.birthday_cards[bid]["redeemed_at"] is None
    assert u.json()["transaction"]["kind"] == "reversal"
    assert "birthdayReward" in _resolve(env).json()
    assert env.pushes[-1] == CARD1


# --------------------------------------------------------------------------- #
# pause: a scan reactivates
# --------------------------------------------------------------------------- #
def test_resolve_reactivates_a_paused_card_with_balance_intact(env):
    env.repo.card_rows[CARD1]["paused_at"] = "2026-06-01T00:00:00+00:00"
    env.repo.cards[CARD1].paused = True
    body = _resolve(env).json()
    assert body["card"]["paused"] is False
    assert body["card"]["balance"]["stamps"] == 7
    assert env.repo.card_rows[CARD1]["paused_at"] is None


def test_accrue_and_redeem_reactivate_a_paused_card(env):
    env.repo.card_rows[CARD1]["paused_at"] = "2026-06-01T00:00:00+00:00"
    env.repo.cards[CARD1].paused = True
    r = env.accrue()
    assert r.json()["card"]["paused"] is False
    assert env.repo.card_rows[CARD1]["paused_at"] is None
    env.repo.card_rows[CARD1]["paused_at"] = "2026-06-01T00:00:00+00:00"
    env.repo.cards[CARD1].paused = True
    assert env.redeem().json()["card"]["paused"] is False
    assert env.repo.card_rows[CARD1]["paused_at"] is None


def test_resolve_inactive_card_keeps_its_behaviour(env):
    env.repo.card_rows[CARD1]["active"] = False
    _birthday(env)
    body = _resolve(env).json()
    assert body["card"]["active"] is False and "birthdayReward" not in body
    env.repo.cards[CARD1].state = CardState(7, 0, 0, False)
    assert env.accrue().json()["details"]["reason"] == "card_inactive"


# --------------------------------------------------------------------------- #
# GET /operations/recent
# --------------------------------------------------------------------------- #
def _recent(env, **params):
    return env.client.get("/operations/recent", params=params)


def test_recent_operation_user_sees_only_their_own_newest_first(env):
    env.repo.customers["c1"] = {"id": "c1", "full_name": "maría Torres Vega"}
    env.repo.card_rows[CARD1]["customer_id"] = "c1"
    a = env.txn(stamps=1, age=50)
    b = env.txn("redeem", age=40)
    env.txn(stamps=1, age=30, who=OP2)
    rows = _recent(env).json()
    assert [r["id"] for r in rows] == [b.id, a.id]
    assert rows[0]["holderName"] == "maría V."
    assert rows[0]["programName"] == "Café"
    assert rows[1]["stampsDelta"] == 1 and rows[1]["reversed"] is False


def test_recent_owner_sees_the_whole_merchant_not_others(env):
    env.as_user(OWNER, ("merchant_owner",))
    env.put_card(CARD2, CardState(1, 0, 0, True), merchant=M2)
    mine = env.txn(stamps=1, age=5, who=OP1)
    other = env.txn(stamps=1, age=4, who=OP2)
    env.txn(card=CARD2, age=3)                              # another merchant
    ids = {r["id"] for r in _recent(env).json()}
    assert ids == {mine.id, other.id}


def test_recent_filters_by_location_and_limit(env):
    env.txn(stamps=1, age=60, location=LOC1)
    env.txn(stamps=1, age=50, location=LOC2)
    env.txn(stamps=1, age=40, location=LOC1)
    assert len(_recent(env, locationId=LOC1).json()) == 2
    assert len(_recent(env, limit=1).json()) == 1
    assert len(_recent(env).json()) == 3
    assert _recent(env, limit=0).status_code == 422
    assert _recent(env, limit=21).status_code == 422


def test_recent_default_limit_is_five(env):
    for i in range(8):
        env.txn(stamps=1, age=100 - i)
    assert len(_recent(env).json()) == 5


def test_recent_excludes_reversals_and_marks_reversed(env):
    done = env.txn(stamps=1, age=10, reversed_at=T0)
    env.txn("reversal", age=9, stamps=-1, reverses_id=done.id)
    env.txn("validate_access", age=8)
    rows = _recent(env).json()
    assert [r["id"] for r in rows] == [done.id]
    assert rows[0]["reversed"] is True and rows[0]["undoable"] is False


def test_recent_undoable_matches_the_undo_rules(env):
    env.put_card(CARD2, CardState(1, 0, 0, True))
    old = env.txn(stamps=1, age=100)                        # outside the window
    shadowed = env.txn(stamps=1, age=20)                    # a newer one exists
    latest = env.txn(stamps=1, age=5)
    theirs = env.txn(stamps=1, age=5, card=CARD2, who=OP2)  # not mine (hidden)
    got = {r["id"]: r["undoable"] for r in _recent(env).json()}
    assert got == {old.id: False, shadowed.id: False, latest.id: True}
    assert theirs.id not in got


def test_recent_holder_name_fallbacks(env):
    env.txn(stamps=1, age=30)
    assert _recent(env).json()[0]["holderName"] == "Cliente"
    env.repo.card_rows[CARD1]["holder_name"] = "Ana P."
    assert _recent(env).json()[0]["holderName"] == "Ana P."
    env.repo.customers["c1"] = {"id": "c1", "full_name": "Luis"}
    env.repo.card_rows[CARD1]["customer_id"] = "c1"
    assert _recent(env).json()[0]["holderName"] == "Luis"


# --------------------------------------------------------------------------- #
# SupabaseRepository against the FakeDB look-alike
# --------------------------------------------------------------------------- #
def _fake_repo():
    db = FakeDB(
        cards=[{"id": CARD1, "program_id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                "customer_id": "c1", "stamps": 2, "points": 0, "rewards_available": 0,
                "rewards_redeemed": 0, "level": 3, "paused_at": "2026-01-01T00:00:00+00:00",
                "holder_name": "Zed Q."}],
        customers=[{"id": "c1", "full_name": "Rosa Pérez Díaz"}],
        programs=[{"id": P1, "name": "Café"}],
        transactions=[
            {"id": "t1", "card_id": CARD1, "merchant_id": M1, "kind": "accrue_stamps",
             "stamps_delta": 1, "points_delta": 0, "operation_user_id": OP1,
             "location_id": LOC1, "created_at": "2026-11-19T15:00:00+00:00"},
            {"id": "t2", "card_id": CARD1, "merchant_id": M1, "kind": "reversal",
             "stamps_delta": -1, "points_delta": 0, "operation_user_id": OP1,
             "created_at": "2026-11-19T15:00:05+00:00", "reverses_id": "t1"}],
        birthday_cards=[{"id": "b1", "card_id": CARD1, "status": "issued", "redeemed_at": None,
                         "valid_until": "2099-01-01", "benefit": "x"}])
    return SupabaseRepository(db), db


def test_supabase_repo_recent_reads_and_skips_reversals():
    repo, _ = _fake_repo()
    rows = repo.list_recent(M1, operation_user_id=OP1, location_id=None, limit=5)
    assert [r["txn"].id for r in rows] == ["t1"]
    assert (rows[0]["full_name"], rows[0]["program_name"]) == ("Rosa Pérez Díaz", "Café")
    assert repo.list_recent(M1, operation_user_id=OP2, location_id=None, limit=5) == []


def test_supabase_repo_card_row_carries_level_and_paused():
    repo, db = _fake_repo()
    card = repo.get_card(CARD1)
    assert card.state.level == 3 and card.paused is True
    assert repo.reactivate_if_paused(CARD1) is True
    assert db.rows("cards")[0]["paused_at"] is None
    assert repo.reactivate_if_paused(CARD1) is False


def test_supabase_repo_commit_writes_level_with_the_balance():
    repo, db = _fake_repo()
    card = repo.get_card(CARD1)
    card.state = logic.redeem(CardState(2, 0, 1, True, 0, level=3), raise_level=True)
    repo.commit(card, TxnRow("t9", CARD1, "redeem", 0, 0, OP1, T0))
    row = db.rows("cards")[0]
    assert (row["level"], row["rewards_redeemed"], row["rewards_available"]) == (4, 1, 0)


def test_supabase_repo_undo_writes_reversal_first_then_marks_original():
    repo, db = _fake_repo()
    card = repo.get_card(CARD1)
    orig = repo.get_transaction("t1")
    reversal = TxnRow("t3", CARD1, "reversal", -1, 0, OP1, T0, LOC1, reverses_id="t1")
    repo.commit_undo(card, reversal, orig, T0)
    t3 = next(r for r in db.rows("transactions") if r["id"] == "t3")
    assert t3["reverses_id"] == "t1"
    t1 = next(r for r in db.rows("transactions") if r["id"] == "t1")
    assert t1["reversed_at"] == T0.isoformat()


def test_supabase_repo_duplicate_reversal_maps_to_domain_error():
    repo, db = _fake_repo()

    class Dup(Exception):
        def __str__(self):
            return "{'code': '23505', 'message': 'duplicate key value'}"

    real = db.table

    def table(name):
        q = real(name)
        if name == "transactions":
            orig = q.insert

            def insert(row):
                if row.get("reverses_id"):
                    raise Dup()
                return orig(row)
            q.insert = insert
        return q
    db.table = table
    card = repo.get_card(CARD1)
    with pytest.raises(DuplicateReversal):
        repo.commit_undo(card, TxnRow("t4", CARD1, "reversal", -1, 0, OP1, T0, None, "t1"),
                         repo.get_transaction("t1"), T0)


def test_supabase_repo_birthday_lookup_and_state():
    repo, db = _fake_repo()
    assert repo.get_birthday_reward(CARD1, today_ec())["id"] == "b1"
    repo.set_birthday_state("b1", "redeemed", T0)
    assert repo.get_birthday_reward(CARD1, today_ec()) is None
    assert repo.find_redeemed_birthday(CARD1, T0)["id"] == "b1"
    repo.set_birthday_state("b1", "issued", None)
    assert db.rows("birthday_cards")[0]["redeemed_at"] is None


def test_supabase_repo_has_newer_effective():
    repo, db = _fake_repo()
    t1 = repo.get_transaction("t1")
    assert repo.has_newer_effective(t1) is False            # only a reversal is newer
    db.rows("transactions").append({
        "id": "t5", "card_id": CARD1, "merchant_id": M1, "kind": "redeem",
        "created_at": "2026-11-19T15:00:10+00:00"})
    assert repo.has_newer_effective(t1) is True
