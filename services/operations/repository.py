"""Persistence boundary for the operations service.

A Protocol so the handler and logic are testable with an in-memory fake offline; the
Supabase implementation is used in deployed stages (Phase 3 completes its wiring).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .logic import CardState, ProgramRules


@dataclass
class CardRow:
    id: str
    program_id: str
    merchant_id: str
    program_type: str
    state: CardState


@dataclass
class TxnRow:
    id: str
    card_id: str
    kind: str
    stamps_delta: int
    points_delta: int
    operation_user_id: str | None
    created_at: datetime


class OperationsRepository(Protocol):
    def get_card(self, card_id: str) -> CardRow | None: ...
    def get_card_by_token(self, token: str) -> CardRow | None: ...
    def get_rules(self, program_id: str) -> ProgramRules | None: ...
    def recent_accrual_count(self, card_id: str, within_seconds: int) -> int: ...
    def commit(self, card: CardRow, txn: TxnRow) -> None: ...
    # Raw-row reads for scan resolution + wallet reflection (Phase 4).
    def get_card_row(self, card_id: str) -> dict | None: ...
    def get_card_row_by_token(self, token: str) -> dict | None: ...
    def get_program(self, program_id: str) -> dict | None: ...
    def get_merchant(self, merchant_id: str) -> dict | None: ...
    # Redemptions report (Phase 6).
    def list_redemptions(self, program_id: str, from_: str | None,
                         to: str | None) -> list[dict]: ...


# --------------------------------------------------------------------------- #
# In-memory implementation — used by unit tests; zero external deps.
# --------------------------------------------------------------------------- #
@dataclass
class InMemoryRepository:
    cards: dict[str, CardRow] = field(default_factory=dict)
    rules: dict[str, ProgramRules] = field(default_factory=dict)
    txns: list[TxnRow] = field(default_factory=list)
    card_rows: dict[str, dict] = field(default_factory=dict)
    programs: dict[str, dict] = field(default_factory=dict)
    merchants: dict[str, dict] = field(default_factory=dict)
    redemptions: list[dict] = field(default_factory=list)

    def get_card_row(self, card_id: str) -> dict | None:
        return self.card_rows.get(card_id)

    def get_card_row_by_token(self, token: str) -> dict | None:
        return next((r for r in self.card_rows.values()
                     if r.get("opaque_token") == token), None)

    def get_program(self, program_id: str) -> dict | None:
        return self.programs.get(program_id)

    def get_merchant(self, merchant_id: str) -> dict | None:
        return self.merchants.get(merchant_id)

    def get_card(self, card_id: str) -> CardRow | None:
        return self.cards.get(card_id)

    def get_card_by_token(self, token: str) -> CardRow | None:
        return next((c for c in self.cards.values()
                    if getattr(c, "_token", None) == token), None)

    def get_rules(self, program_id: str) -> ProgramRules | None:
        return self.rules.get(program_id)

    def recent_accrual_count(self, card_id: str, within_seconds: int) -> int:
        cutoff = datetime.now(UTC).timestamp() - within_seconds
        return sum(1 for t in self.txns
                  if t.card_id == card_id
                  and t.kind.startswith("accrue")
                  and t.created_at.timestamp() >= cutoff)

    def commit(self, card: CardRow, txn: TxnRow) -> None:
        self.cards[card.id] = card
        self.txns.append(txn)

    def list_redemptions(self, program_id: str, from_: str | None,
                         to: str | None) -> list[dict]:
        rows = [r for r in self.redemptions if r.get("program_id") == program_id]
        if from_ is not None:
            rows = [r for r in rows if r["redeemed_at"] >= from_]
        if to is not None:
            rows = [r for r in rows if r["redeemed_at"] <= to]
        return sorted(rows, key=lambda r: r["redeemed_at"], reverse=True)


# --------------------------------------------------------------------------- #
# Supabase implementation — real persistence (completed in Phase 3, needs creds).
# --------------------------------------------------------------------------- #
class SupabaseRepository:
    def __init__(self, client=None):
        self._client = client

    def _c(self):
        if self._client is None:
            from conpass_common.db import service_client
            self._client = service_client()
        return self._client

    def get_card(self, card_id: str) -> CardRow | None:
        rows = self._c().table("cards").select("*").eq("id", card_id).execute().data
        return _row_to_card(rows[0]) if rows else None

    def get_card_by_token(self, token: str) -> CardRow | None:
        rows = self._c().table("cards").select("*").eq("opaque_token", token).execute().data
        return _row_to_card(rows[0]) if rows else None

    def get_card_row(self, card_id: str) -> dict | None:
        rows = self._c().table("cards").select("*").eq("id", card_id).execute().data
        return rows[0] if rows else None

    def get_card_row_by_token(self, token: str) -> dict | None:
        rows = self._c().table("cards").select("*").eq("opaque_token", token).execute().data
        return rows[0] if rows else None

    def get_program(self, program_id: str) -> dict | None:
        rows = self._c().table("programs").select("*").eq("id", program_id).execute().data
        return rows[0] if rows else None

    def get_merchant(self, merchant_id: str) -> dict | None:
        rows = self._c().table("merchants").select("*").eq("id", merchant_id).execute().data
        return rows[0] if rows else None

    def get_rules(self, program_id: str) -> ProgramRules | None:
        rows = self._c().table("programs").select(
            "type,mechanic,stamps_for_reward,points_for_reward,points_per_dollar"
        ).eq("id", program_id).execute().data
        if not rows:
            return None
        p = rows[0]
        return ProgramRules(p["type"], p.get("mechanic"), p.get("stamps_for_reward"),
                            p.get("points_for_reward"), p.get("points_per_dollar"))

    def recent_accrual_count(self, card_id: str, within_seconds: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=within_seconds)).isoformat()
        res = (self._c().table("transactions").select("id", count="exact")
               .eq("card_id", card_id).gte("created_at", cutoff)
               .in_("kind", ["accrue_stamps", "accrue_points"]).execute())
        return res.count or 0

    def commit(self, card: CardRow, txn: TxnRow) -> None:  # pragma: no cover - Phase 3
        c = self._c()
        s = card.state
        c.table("cards").update({
            "stamps": s.stamps, "points": s.points,
            "rewards_available": s.rewards_available,
        }).eq("id", card.id).execute()
        c.table("transactions").insert({
            "id": txn.id, "card_id": txn.card_id, "merchant_id": card.merchant_id,
            "kind": txn.kind, "stamps_delta": txn.stamps_delta,
            "points_delta": txn.points_delta, "operation_user_id": txn.operation_user_id,
        }).execute()

    def list_redemptions(self, program_id: str, from_: str | None,
                         to: str | None) -> list[dict]:
        q = (self._c().table("redemptions_view").select("*")
             .eq("program_id", program_id))
        if from_ is not None:
            q = q.gte("redeemed_at", from_)
        if to is not None:
            q = q.lte("redeemed_at", to)
        return q.order("redeemed_at", desc=True).execute().data


def _row_to_card(r: dict) -> CardRow:
    muntil = r.get("membership_active_until")
    return CardRow(
        id=r["id"], program_id=r["program_id"], merchant_id=r["merchant_id"],
        program_type=r["type"],
        state=CardState(
            stamps=r.get("stamps", 0), points=r.get("points", 0),
            rewards_available=r.get("rewards_available", 0), active=r.get("active", True),
            membership_active_until=date.fromisoformat(muntil) if muntil else None,
        ),
    )
