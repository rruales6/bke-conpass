"""Never-saved detection (Phase 18, D21; model v3.0 §4): poll Google's object `hasUsers`.

A card counts for billing and notifications only once its holder saved the pass
(`cards.wallet_installed`, read by the `merchant_active_passes` and `notifiable_cards` views).
Google has no cheap push for this without class callbacks, so the notifier's minute job asks
for a rotating batch:
- active, not-yet-saved cards enrolled in the last POLL_WINDOW_DAYS;
- least recently checked first (`cards.wallet_checked_at`), newest enrolments first among ties.
A card is never flipped back to unsaved here: removing a saved pass is a separate signal
(BACKLOG).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .logging import get_logger
from .providers.factory import get_wallet_provider

log = get_logger(__name__)

POLL_WINDOW_DAYS = 30


def poll_unsaved(client, limit: int = 40, *, now: datetime | None = None, provider=None) -> int:
    """Check up to `limit` unsaved cards and mark those Google reports as saved. Returns how many
    were marked. Best-effort: a provider failure checks nothing and raises nothing."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=POLL_WINDOW_DAYS)).isoformat()
    rows = (client.table("cards").select("id")
            .eq("active", True).eq("wallet_installed", False).gte("created_at", since)
            .order("wallet_checked_at", desc=False, nullsfirst=True)
            .order("created_at", desc=True)
            .limit(limit).execute().data or [])
    ids = [str(r["id"]) for r in rows]
    if not ids:
        return 0
    try:
        saved = (provider or get_wallet_provider()).saved_cards(ids)
    except Exception:  # noqa: BLE001 - not configured / auth failure: try again next minute
        log.warning("wallet saved-state poll failed", exc_info=True)
        return 0
    checked = [i for i in ids if i in saved]
    if checked:
        (client.table("cards").update({"wallet_checked_at": now.isoformat()})
         .in_("id", checked).execute())
    newly = [i for i, ok in saved.items() if ok]
    if newly:
        client.table("cards").update({"wallet_installed": True}).in_("id", newly).execute()
    return len(newly)
