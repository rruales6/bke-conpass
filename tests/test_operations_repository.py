"""Repository-level tests for `SupabaseRepository` — verifies the mapping between DB
rows and `CardState` survives a round trip through `_row_to_card()` and `commit()`.
A small fake Data API client (extending the `_FakeQuery`/`_FakeClient` pattern used
elsewhere, e.g. test_wallet.py / test_metrics_admin.py) stands in for supabase-py so
this stays network-free."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from services.operations.logic import CardState, redeem
from services.operations.repository import CardRow, SupabaseRepository, TxnRow

CARD1 = "44444444-4444-4444-4444-444444444444"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
OP1 = "55555555-5555-5555-5555-555555555555"


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self._store, self._name, self._filter = store, name, {}
        self._update = self._insert = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def update(self, patch):
        self._update = patch
        return self

    def insert(self, row):
        self._insert = row
        return self

    def execute(self):
        rows = self._store.setdefault(self._name, [])
        if self._update is not None:
            for r in rows:
                if all(r.get(k) == v for k, v in self._filter.items()):
                    r.update(self._update)
        elif self._insert is not None:
            rows.append(dict(self._insert))
        result = [r for r in rows if all(r.get(k) == v for k, v in self._filter.items())]
        return type("Result", (), {"data": result})()


class _FakeClient:
    def __init__(self, tables: dict):
        self._tables = tables

    def table(self, name: str):
        return _FakeTable(self._tables, name)


def test_rewards_redeemed_round_trips_through_commit_and_row_to_card():
    # A card with 4 past redemptions (e.g. backfilled by the migration) and 2 rewards
    # still available.
    tables = {
        "cards": [{
            "id": CARD1, "program_id": P1, "merchant_id": M1, "type": "loyalty_stamps",
            "stamps": 0, "points": 0, "rewards_available": 2, "rewards_redeemed": 4,
            "active": True,
        }],
        "transactions": [],
    }
    repo = SupabaseRepository(client=_FakeClient(tables))

    card = repo.get_card(CARD1)
    assert card.state.rewards_redeemed == 4   # read back with the same value it was seeded with

    card.state = redeem(card.state)
    txn = TxnRow(str(uuid.uuid4()), card.id, "redeem", 0, 0, OP1, datetime.now(UTC))
    repo.commit(card, txn)

    # The row actually persisted in the (fake) DB reflects the increment...
    row = tables["cards"][0]
    assert row["rewards_available"] == 1
    assert row["rewards_redeemed"] == 5
    assert tables["transactions"][0]["kind"] == "redeem"

    # ...and re-reading it through _row_to_card gives back the same CardState field.
    reread = repo.get_card(CARD1)
    assert reread.state.rewards_redeemed == 5


def test_row_to_card_defaults_rewards_redeemed_when_column_absent():
    # Cards created before the backfill migration ran (or any row missing the column
    # for some other reason) must not blow up — default to 0, same style as the
    # other balance fields.
    tables = {"cards": [{
        "id": CARD1, "program_id": P1, "merchant_id": M1, "type": "loyalty_stamps",
        "stamps": 0, "points": 0, "rewards_available": 0, "active": True,
    }]}
    repo = SupabaseRepository(client=_FakeClient(tables))
    card = repo.get_card(CARD1)
    assert card.state.rewards_redeemed == 0


def test_commit_persists_rewards_redeemed_field(monkeypatch):
    # commit() must write rewards_redeemed, not just stamps/points/rewards_available.
    tables = {"cards": [{
        "id": CARD1, "program_id": P1, "merchant_id": M1, "type": "loyalty_stamps",
        "stamps": 0, "points": 0, "rewards_available": 0, "rewards_redeemed": 0,
        "active": True,
    }], "transactions": []}
    repo = SupabaseRepository(client=_FakeClient(tables))
    card = CardRow(CARD1, P1, M1, "loyalty_stamps",
                   CardState(stamps=0, points=0, rewards_available=0, active=True,
                            rewards_redeemed=9))
    txn = TxnRow(str(uuid.uuid4()), CARD1, "redeem", 0, 0, OP1, datetime.now(UTC))
    repo.commit(card, txn)
    assert tables["cards"][0]["rewards_redeemed"] == 9
