"""Outbox primitives over `notifications` / `notification_deliveries` (Phase 16).

Everything that puts a row into the outbox goes through here so the rules live once:
one automation `notifications` row per (merchant, type, Ecuador day) that every card's
delivery hangs off; a fresh balance/near message supersedes the card's stale pending one;
time-based deliveries are deduplicated by `dedupe_key` (insert-or-ignore).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import date

SUPERSEDABLE_TYPES = ("balance", "near")
PAGE = 1000          # PostgREST caps a response at 1000 rows by default
BATCH = 200


def chunks(items: list, size: int = BATCH) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_all(build: Callable[[], object]) -> list[dict]:
    """Every row of a query, paged. `build()` must return a FRESH query each call
    (PostgREST builders are single-use once a range is applied)."""
    out: list[dict] = []
    start = 0
    while True:
        rows = build().range(start, start + PAGE - 1).execute().data or []
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        start += PAGE


def get_or_create_automation(client, merchant_id: str, type_: str, day: date,
                             header: str, sample_body: str) -> str:
    """The day's batch row for an automation type, created on first use.

    The unique index is partial (source = 'automation'), which PostgREST's on_conflict
    cannot target, so: select, insert, and on a lost race select again. A batch that the
    dispatcher already closed (`sent`) is reopened because a new delivery joins it.
    """
    def find() -> dict | None:
        rows = (client.table("notifications").select("id,status")
                .eq("merchant_id", merchant_id).eq("source", "automation")
                .eq("type", type_).eq("day", day.isoformat()).limit(1).execute().data or [])
        return rows[0] if rows else None

    row = find()
    if row is None:
        try:
            created = client.table("notifications").insert({
                "merchant_id": merchant_id, "source": "automation", "type": type_,
                "audience": "automatic", "title": header[:60], "body": sample_body[:300],
                "status": "queued", "day": day.isoformat()}).execute().data
            return created[0]["id"]
        except Exception:  # noqa: BLE001 - unique violation: another writer won the race
            row = find()
            if row is None:
                raise
    if row["status"] != "queued":
        client.table("notifications").update({"status": "queued", "sent_at": None}).eq(
            "id", row["id"]).execute()
    return row["id"]


def supersede_pending(client, card_id: str) -> int:
    """Mark the card's still-pending balance/near deliveries `superseded`: a stale
    "le faltan 2" must never go out after the next visit. Returns how many."""
    pending = (client.table("notification_deliveries").select("id,notification_id")
               .eq("card_id", card_id).eq("status", "pending").execute().data or [])
    if not pending:
        return 0
    ids = list({p["notification_id"] for p in pending})
    stale = (client.table("notifications").select("id").in_("id", ids)
             .in_("type", list(SUPERSEDABLE_TYPES)).execute().data or [])
    stale_ids = {s["id"] for s in stale}
    victims = [p["id"] for p in pending if p["notification_id"] in stale_ids]
    if victims:
        client.table("notification_deliveries").update({"status": "superseded"}).in_(
            "id", victims).execute()
    return len(victims)


def enqueue_event(client, *, merchant_id: str, card_id: str, type_: str, header: str,
                  body: str, day: date) -> str:
    """Queue ONE event-automation delivery for a card (after an accrual).

    Supersedes first so a balance message never supersedes itself. Returns the delivery id.
    """
    notification_id = get_or_create_automation(client, merchant_id, type_, day, header, body)
    supersede_pending(client, card_id)
    created = client.table("notification_deliveries").insert({
        "notification_id": notification_id, "merchant_id": merchant_id, "card_id": card_id,
        "header": header, "body": body}).execute().data
    return created[0]["id"]


def enqueue_timed(client, *, merchant_id: str, type_: str, day: date, header: str,
                  rows: Iterable[dict]) -> int:
    """Queue time-based deliveries (expiry warning / inactivity) for one merchant.

    `rows` are `{card_id, body, dedupe_key}`. Insert-or-ignore on `dedupe_key`, so a daily
    re-run (or an overlapping one) never double-sends. Returns rows actually inserted.
    """
    rows = list(rows)
    if not rows:
        return 0
    notification_id = get_or_create_automation(client, merchant_id, type_, day, header,
                                               rows[0]["body"])
    inserted = 0
    for batch in chunks(rows):
        payload = [{"notification_id": notification_id, "merchant_id": merchant_id,
                    "card_id": r["card_id"], "header": header, "body": r["body"],
                    "dedupe_key": r["dedupe_key"]} for r in batch]
        res = client.table("notification_deliveries").upsert(
            payload, on_conflict="dedupe_key", ignore_duplicates=True).execute().data or []
        inserted += len(res)
    return inserted
