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
    paused: bool = False


class DuplicateReversal(Exception):
    """A reversal already exists for that transaction (unique index on reverses_id)."""


@dataclass
class TxnRow:
    id: str
    card_id: str
    kind: str
    stamps_delta: int
    points_delta: int
    operation_user_id: str | None
    created_at: datetime
    location_id: str | None = None   # locale of the scan (Phase 16)
    reverses_id: str | None = None   # kind 'reversal': the operation it undoes (Phase 18)
    reversed_at: datetime | None = None   # set on an operation that was undone


class OperationsRepository(Protocol):
    def get_card(self, card_id: str) -> CardRow | None: ...
    def get_card_by_token(self, token: str) -> CardRow | None: ...
    def get_rules(self, program_id: str) -> ProgramRules | None: ...
    def recent_accrual_count(self, card_id: str, within_seconds: int) -> int: ...
    def commit(self, card: CardRow, txn: TxnRow) -> None: ...
    # Phase 18: pause, birthday rewards, undo, recent operations.
    def reactivate_if_paused(self, card_id: str) -> bool: ...
    def get_birthday_reward(self, card_id: str, today: date) -> dict | None: ...
    def get_birthday_card(self, birthday_id: str) -> dict | None: ...
    def find_redeemed_birthday(self, card_id: str, at: datetime) -> dict | None: ...
    def set_birthday_state(self, birthday_id: str, status: str,
                           redeemed_at: datetime | None) -> None: ...
    def get_transaction(self, txn_id: str) -> TxnRow | None: ...
    def has_newer_effective(self, txn: TxnRow) -> bool: ...
    def commit_undo(self, card: CardRow, reversal: TxnRow, original: TxnRow,
                    reversed_at: datetime) -> None: ...
    def list_recent(self, merchant_id: str, *, operation_user_id: str | None,
                    location_id: str | None, limit: int) -> list[dict]: ...
    # Raw-row reads for scan resolution + wallet reflection (Phase 4).
    def get_card_row(self, card_id: str) -> dict | None: ...
    def get_card_row_by_token(self, token: str) -> dict | None: ...
    def get_program(self, program_id: str) -> dict | None: ...
    def get_merchant(self, merchant_id: str) -> dict | None: ...
    def get_customer(self, customer_id: str) -> dict | None: ...
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
    customers: dict[str, dict] = field(default_factory=dict)
    redemptions: list[dict] = field(default_factory=list)
    birthday_cards: dict[str, dict] = field(default_factory=dict)

    def reactivate_if_paused(self, card_id: str) -> bool:
        card = self.cards.get(card_id)
        was = bool(card and card.paused)
        if card:
            card.paused = False
        row = self.card_rows.get(card_id)
        if row is not None and row.get("paused_at"):
            row["paused_at"] = None
            was = True
        return was

    def get_birthday_reward(self, card_id: str, today: date) -> dict | None:
        rows = [b for b in self.birthday_cards.values()
                if b.get("card_id") == card_id and b.get("status") == "issued"
                and not b.get("redeemed_at")
                and b.get("valid_until") and str(b["valid_until"])[:10] >= today.isoformat()]
        return rows[0] if rows else None

    def get_birthday_card(self, birthday_id: str) -> dict | None:
        return self.birthday_cards.get(birthday_id)

    def find_redeemed_birthday(self, card_id: str, at: datetime) -> dict | None:
        return _latest_redeemed([b for b in self.birthday_cards.values()
                                 if b.get("card_id") == card_id])

    def set_birthday_state(self, birthday_id: str, status: str,
                           redeemed_at: datetime | None) -> None:
        row = self.birthday_cards[birthday_id]
        row["status"] = status
        row["redeemed_at"] = redeemed_at.isoformat() if redeemed_at else None

    def get_transaction(self, txn_id: str) -> TxnRow | None:
        return next((t for t in self.txns if t.id == txn_id), None)

    def has_newer_effective(self, txn: TxnRow) -> bool:
        return any(t.card_id == txn.card_id and t.id != txn.id
                   and t.created_at > txn.created_at
                   and t.kind != "reversal" and t.reversed_at is None
                   for t in self.txns)

    def commit_undo(self, card: CardRow, reversal: TxnRow, original: TxnRow,
                    reversed_at: datetime) -> None:
        if any(t.reverses_id == original.id for t in self.txns):
            raise DuplicateReversal(original.id)
        self.txns.append(reversal)
        original.reversed_at = reversed_at
        self.cards[card.id] = card

    def list_recent(self, merchant_id: str, *, operation_user_id: str | None,
                    location_id: str | None, limit: int) -> list[dict]:
        out = []
        for t in sorted(self.txns, key=lambda t: t.created_at, reverse=True):
            card = self.cards.get(t.card_id)
            if (t.kind not in RECENT_KINDS or card is None
                    or card.merchant_id != merchant_id
                    or (operation_user_id and t.operation_user_id != operation_user_id)
                    or (location_id and t.location_id != location_id)):
                continue
            row = self.card_rows.get(t.card_id) or {}
            customer = self.customers.get(row.get("customer_id") or "") or {}
            program = self.programs.get(card.program_id) or {}
            out.append(_recent_row(t, customer.get("full_name"), row.get("holder_name"),
                                   program.get("name")))
            if len(out) >= limit:
                break
        return out

    def get_card_row(self, card_id: str) -> dict | None:
        return self.card_rows.get(card_id)

    def get_card_row_by_token(self, token: str) -> dict | None:
        return next((r for r in self.card_rows.values()
                     if r.get("opaque_token") == token), None)

    def get_program(self, program_id: str) -> dict | None:
        return self.programs.get(program_id)

    def get_merchant(self, merchant_id: str) -> dict | None:
        return self.merchants.get(merchant_id)

    def get_customer(self, customer_id: str) -> dict | None:
        return self.customers.get(customer_id)

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

    def get_customer(self, customer_id: str) -> dict | None:
        rows = self._c().table("customers").select("*").eq(
            "id", customer_id).execute().data
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
        # One card update carries balance AND level, so a redemption can't half-apply.
        c.table("cards").update(_card_update(card)).eq("id", card.id).execute()
        c.table("transactions").insert(_txn_insert(card, txn)).execute()

    def reactivate_if_paused(self, card_id: str) -> bool:
        from conpass_common import card_lifecycle

        rows = self._c().table("cards").select("id,paused_at").eq(
            "id", card_id).execute().data
        return bool(rows) and card_lifecycle.reactivate_if_paused(self._c(), rows[0])

    def get_birthday_reward(self, card_id: str, today: date) -> dict | None:
        rows = (self._c().table("birthday_cards").select("*").eq("card_id", card_id)
                .eq("status", "issued").gte("valid_until", today.isoformat())
                .execute().data or [])
        rows = [r for r in rows if not r.get("redeemed_at")]
        return rows[0] if rows else None

    def get_birthday_card(self, birthday_id: str) -> dict | None:
        rows = self._c().table("birthday_cards").select("*").eq(
            "id", birthday_id).execute().data
        return rows[0] if rows else None

    def set_birthday_state(self, birthday_id: str, status: str,
                           redeemed_at: datetime | None) -> None:
        self._c().table("birthday_cards").update({
            "status": status,
            "redeemed_at": redeemed_at.isoformat() if redeemed_at else None,
        }).eq("id", birthday_id).execute()

    def find_redeemed_birthday(self, card_id: str, at: datetime) -> dict | None:
        rows = (self._c().table("birthday_cards").select("*").eq("card_id", card_id)
                .eq("status", "redeemed").execute().data or [])
        return _latest_redeemed(rows)

    def get_transaction(self, txn_id: str) -> TxnRow | None:
        rows = self._c().table("transactions").select("*").eq("id", txn_id).execute().data
        return _row_to_txn(rows[0]) if rows else None

    def has_newer_effective(self, txn: TxnRow) -> bool:
        rows = (self._c().table("transactions").select("id,kind,reversed_at,created_at")
                .eq("card_id", txn.card_id).gte("created_at", txn.created_at.isoformat())
                .execute().data or [])
        return any(r["id"] != txn.id and r["kind"] != "reversal" and not r.get("reversed_at")
                   and _ts(r["created_at"]) > txn.created_at for r in rows)

    def commit_undo(self, card: CardRow, reversal: TxnRow, original: TxnRow,
                    reversed_at: datetime) -> None:
        c = self._c()
        try:
            c.table("transactions").insert(_txn_insert(card, reversal)).execute()
        except Exception as exc:  # noqa: BLE001 - classify the unique violation below
            if "23505" in str(exc) or "duplicate key" in str(exc).lower():
                raise DuplicateReversal(original.id) from exc
            raise
        c.table("transactions").update({"reversed_at": reversed_at.isoformat()}).eq(
            "id", original.id).execute()
        c.table("cards").update(_card_update(card)).eq("id", card.id).execute()

    def list_recent(self, merchant_id: str, *, operation_user_id: str | None,
                    location_id: str | None, limit: int) -> list[dict]:
        c = self._c()
        q = (c.table("transactions").select("*").eq("merchant_id", merchant_id)
             .in_("kind", list(RECENT_KINDS)))
        if operation_user_id:
            q = q.eq("operation_user_id", operation_user_id)
        if location_id:
            q = q.eq("location_id", location_id)
        txns = [_row_to_txn(r) for r in
                q.order("created_at", desc=True).limit(limit).execute().data or []]
        if not txns:
            return []
        cards = {r["id"]: r for r in c.table("cards").select("*").in_(
            "id", sorted({t.card_id for t in txns})).execute().data or []}
        customer_ids = sorted({r["customer_id"] for r in cards.values() if r.get("customer_id")})
        customers = {r["id"]: r for r in (c.table("customers").select("id,full_name").in_(
            "id", customer_ids).execute().data or [])} if customer_ids else {}
        program_ids = sorted({r["program_id"] for r in cards.values()})
        programs = {r["id"]: r for r in (c.table("programs").select("id,name").in_(
            "id", program_ids).execute().data or [])} if program_ids else {}
        out = []
        for t in txns:
            card = cards.get(t.card_id) or {}
            out.append(_recent_row(
                t, (customers.get(card.get("customer_id")) or {}).get("full_name"),
                card.get("holder_name"), (programs.get(card.get("program_id")) or {}).get("name")))
        return out

    def list_redemptions(self, program_id: str, from_: str | None,
                         to: str | None) -> list[dict]:
        q = (self._c().table("redemptions_view").select("*")
             .eq("program_id", program_id))
        if from_ is not None:
            q = q.gte("redeemed_at", from_)
        if to is not None:
            q = q.lte("redeemed_at", to)
        return q.order("redeemed_at", desc=True).execute().data


RECENT_KINDS = ("accrue_stamps", "accrue_points", "redeem", "birthday_redeem")


def _latest_redeemed(rows: list[dict]) -> dict | None:
    """The birthday reward redeemed most recently: with the card's latest operation being the
    birthday redemption (the undo rule), that is the one it spent."""
    rows = [r for r in rows if r.get("status") == "redeemed" and r.get("redeemed_at")]
    return max(rows, key=lambda r: _ts(r["redeemed_at"]), default=None)


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _card_update(card: CardRow) -> dict:
    s = card.state
    return {"stamps": s.stamps, "points": s.points,
            "rewards_available": s.rewards_available,
            "rewards_redeemed": s.rewards_redeemed, "level": s.level}


def _txn_insert(card: CardRow, txn: TxnRow) -> dict:
    row = {
        "id": txn.id, "card_id": txn.card_id, "merchant_id": card.merchant_id,
        "kind": txn.kind, "stamps_delta": txn.stamps_delta,
        "points_delta": txn.points_delta, "operation_user_id": txn.operation_user_id,
        "location_id": txn.location_id,
    }
    if txn.reverses_id:
        row["reverses_id"] = txn.reverses_id
    return row


def _row_to_txn(r: dict) -> TxnRow:
    return TxnRow(
        id=r["id"], card_id=r["card_id"], kind=r["kind"],
        stamps_delta=r.get("stamps_delta") or 0, points_delta=r.get("points_delta") or 0,
        operation_user_id=r.get("operation_user_id"), created_at=_ts(r["created_at"]),
        location_id=r.get("location_id"), reverses_id=r.get("reverses_id"),
        reversed_at=_ts(r["reversed_at"]) if r.get("reversed_at") else None)


def _recent_row(t: TxnRow, full_name: str | None, holder_name: str | None,
                program_name: str | None) -> dict:
    return {"txn": t, "full_name": full_name, "holder_name": holder_name,
            "program_name": program_name}


def _row_to_card(r: dict) -> CardRow:
    muntil = r.get("membership_active_until")
    return CardRow(
        id=r["id"], program_id=r["program_id"], merchant_id=r["merchant_id"],
        program_type=r["type"],
        state=CardState(
            stamps=r.get("stamps", 0), points=r.get("points", 0),
            rewards_available=r.get("rewards_available", 0), active=r.get("active", True),
            membership_active_until=date.fromisoformat(muntil) if muntil else None,
            rewards_redeemed=r.get("rewards_redeemed", 0),
            level=r.get("level") or 1,
        ),
        paused=bool(r.get("paused_at")),
    )
