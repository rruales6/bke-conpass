"""Metered usage (Phase 16): notifications billed this period.

Only `sent` deliveries (push actually delivered) count; silent, skipped and failed ones
are never billed (design: Billing). The period is the calendar month in UTC, same as cards.
"""
from __future__ import annotations

from datetime import date

_PAGE = 1000   # PostgREST's default row cap; page explicitly so big merchants aren't truncated


def _since_iso(since: date) -> str:
    return f"{since.isoformat()}T00:00:00+00:00"


def notifications_used(client, merchant_id: str, since: date) -> int:
    """Pushes delivered to this merchant's customers since `since` (UTC midnight)."""
    res = (client.table("notification_deliveries").select("id", count="exact")
           .eq("merchant_id", merchant_id).eq("status", "sent")
           .gte("sent_at", _since_iso(since)).execute())
    return res.count if res.count is not None else len(res.data or [])


def notifications_used_by_merchant(client, since: date) -> dict[str, int]:
    """merchant_id -> delivered pushes since `since`, for every merchant (admin estimates)."""
    counts: dict[str, int] = {}
    start = 0
    while True:
        rows = (client.table("notification_deliveries").select("merchant_id")
                .eq("status", "sent").gte("sent_at", _since_iso(since))
                .order("id").range(start, start + _PAGE - 1).execute().data or [])
        for r in rows:
            counts[r["merchant_id"]] = counts.get(r["merchant_id"], 0) + 1
        if len(rows) < _PAGE:
            return counts
        start += _PAGE
