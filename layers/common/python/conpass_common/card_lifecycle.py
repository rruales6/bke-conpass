"""Card pause / reactivation (Phase 18, D21 O2): 90 days without use → paused.

A paused card is neither billed nor notified, KEEPS its balance, and the next scan reactivates
it. "Use" = an effective accrual or redemption (card_last_activity, which skips undone
operations), or enrolment when never used.

OWNER: Wave-1 agent B4 (operations / enrollment / identity). The notifier (B2) calls
`pause_inactive_cards`; operations calls `reactivate_if_paused`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .logging import get_logger
from .plans import PAUSE_AFTER_DAYS

log = get_logger(__name__)

_BATCH = 200   # ids per UPDATE ... IN (...) — keeps the PostgREST URL short


def _iso(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def pause_inactive_cards(client, now: datetime | None = None) -> int:
    """Set cards.paused_at = now for active, unpaused cards whose last use is older than
    plans.PAUSE_AFTER_DAYS. Returns the number paused. Idempotent."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=PAUSE_AFTER_DAYS)
    stale = (client.table("card_last_activity").select("card_id,last_at")
             .lt("last_at", cutoff.isoformat()).execute().data or [])
    stale_ids = [r["card_id"] for r in stale if _iso(r["last_at"]) < cutoff]
    if not stale_ids:
        return 0
    # Only active, still-unpaused cards: re-running never re-stamps a paused card.
    eligible: list[str] = []
    for i in range(0, len(stale_ids), _BATCH):
        rows = (client.table("cards").select("id,active,paused_at")
                .in_("id", stale_ids[i:i + _BATCH]).execute().data or [])
        eligible += [r["id"] for r in rows if r.get("active") and not r.get("paused_at")]
    for i in range(0, len(eligible), _BATCH):
        client.table("cards").update({"paused_at": now.isoformat()}).in_(
            "id", eligible[i:i + _BATCH]).execute()
    if eligible:
        log.info("cards_paused", extra={"count": len(eligible)})
    return len(eligible)


def reactivate_if_paused(client, card: dict) -> bool:
    """Clear paused_at when the card is paused (the scan counts as use). Returns True when it
    was paused. Mutates `card["paused_at"]` to None so callers can re-render it."""
    if not card.get("paused_at"):
        return False
    client.table("cards").update({"paused_at": None}).eq("id", card["id"]).execute()
    card["paused_at"] = None
    return True
