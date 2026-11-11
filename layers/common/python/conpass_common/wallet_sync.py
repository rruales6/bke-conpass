"""Wallet sync — the ONE path from DB rows to the wallet (Phase 16, wallet specialist).

The Google object and class are written with full PUTs (an omitted field is CLEARED —
that is what lets a removed image actually disappear, Phase 11). So everything a pass
shows must be re-derived from the DB on every write: balances, level, the expiry date,
the back-of-pass messages (sent outbox rows) and the nearby locations. Feature code goes
through these helpers instead of building PassContent by hand, otherwise one stray write
would silently wipe a card's messages or a program's locales.

    load_card_content(client, card_id)         -> PassContent | None
    content_for(client, card, program, merchant, customer, ctx=None) -> PassContent
    push_card(client, card_id)                  full object refresh (raises on wallet error)
    issue_card(client, card_id)                 create/refresh object + class → IssuedPass
    card_add_link(client, card_id)              re-mint the save link (no writes)
    send_card_message(client, card_id, msg, notify=True)
                                                add a message (+ lock-screen push);
                                                raises WalletQuotaExceeded
    sync_program(client, program_id)            re-PUT one program's class
    sync_merchant_classes(client, merchant_id)  re-PUT every class of a merchant (locales
                                                or plan changed); best-effort, never raises
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .campaigns import ECUADOR_TZ
from .db import service_client
from .notify.settings import (
    MAX_ACTIVE_LOCATIONS,
    MerchantNotificationSettings,
    load_merchant_settings,
)
from .plans import NEARBY_NOTIFICATIONS, get_plan, tier_for_merchant
from .providers import get_wallet_provider
from .providers.wallet import IssuedPass, PassContent, PassMessage, build_pass_content

log = logging.getLogger(__name__)

LOYALTY_TYPES = ("loyalty_stamps", "loyalty_points")
MAX_PASS_MESSAGES = 10          # Google: 10 messages per object
SENT_STATUSES = ("sent", "sent_silent")


@dataclass(frozen=True)
class MerchantContext:
    """Per-merchant facts every pass of that merchant shares — loaded once per batch."""

    settings: MerchantNotificationSettings
    nearby: bool                                   # plan includes nearby notifications
    class_locations: tuple[tuple[float, float], ...]
    locations_by_id: dict                          # active locales, id -> (lat, lng)


def _db(client):
    """Callers may pass None: the service client is then resolved only when a lookup
    actually runs (handler unit tests patch the lookups and never get here)."""
    return client if client is not None else service_client()


def merchant_context(client, merchant_id: str) -> MerchantContext:
    client = _db(client)
    settings = load_merchant_settings(client, merchant_id)
    try:
        nearby = get_plan(tier_for_merchant(client, merchant_id)).has(NEARBY_NOTIFICATIONS)
    except Exception:  # noqa: BLE001 - no subscription row → treat as no nearby
        nearby = False
    rows = (client.table("merchant_locations").select("id,latitude,longitude")
            .eq("merchant_id", merchant_id).eq("active", True)
            .order("created_at").execute().data or [])
    by_id = {r["id"]: (float(r["latitude"]), float(r["longitude"])) for r in rows}
    class_points = tuple(list(by_id.values())[:MAX_ACTIVE_LOCATIONS]) if nearby else ()
    return MerchantContext(settings, nearby, class_points, by_id)


def _card_messages(client, card_id: str, now: datetime) -> tuple[PassMessage, ...]:
    client = _db(client)
    rows = (client.table("notification_deliveries")
            .select("id,header,body,url,display_until,sent_at")
            .eq("card_id", card_id).in_("status", list(SENT_STATUSES))
            .order("sent_at", desc=True).limit(MAX_PASS_MESSAGES * 2).execute().data or [])
    out: list[PassMessage] = []
    for r in rows:
        until = _parse_ts(r.get("display_until"))
        if until is not None and until <= now:
            continue   # Google would hide it anyway; don't spend one of the 10 slots
        out.append(PassMessage(id=str(r["id"]), header=r["header"], body=r["body"],
                               url=r.get("url"), display_until=until))
        if len(out) == MAX_PASS_MESSAGES:
            break
    return tuple(out)


def _card_locations(client, card: dict, ctx: MerchantContext) -> tuple:
    """Per-card nearby points: where the customer signed up, then where they scan most."""
    if not ctx.nearby:
        return ()
    ids: list[str] = []
    s = ctx.settings
    enrolled = card.get("enrolled_location_id")
    if s.card_locations_enrolled and enrolled in ctx.locations_by_id:
        ids.append(enrolled)
    if s.card_locations_top:
        rows = (_db(client).table("transactions").select("location_id")
                .eq("card_id", card["id"]).not_.is_("location_id", "null")
                .execute().data or [])
        for loc_id, _ in Counter(r["location_id"] for r in rows).most_common():
            if loc_id in ctx.locations_by_id and loc_id not in ids:
                ids.append(loc_id)
    return tuple(ctx.locations_by_id[i] for i in ids[:MAX_ACTIVE_LOCATIONS])


def _balance_expires_on(client, card: dict, program: dict,
                        ctx: MerchantContext) -> str | None:
    days = ctx.settings.balance_expiry_days
    if (not days or program.get("type") not in LOYALTY_TYPES
            or not ((card.get("stamps") or 0) > 0 or (card.get("points") or 0) > 0)):
        return None
    rows = (_db(client).table("card_last_activity").select("last_at")
            .eq("card_id", card["id"]).limit(1).execute().data or [])
    last = _parse_ts(rows[0]["last_at"]) if rows else None
    if last is None:
        return None
    return (last + timedelta(days=days)).astimezone(ECUADOR_TZ).date().isoformat()


def content_for(client, card: dict, program: dict, merchant: dict | None,
                customer: dict | None, ctx: MerchantContext | None = None,
                *, now: datetime | None = None) -> PassContent:
    """PassContent for rows the caller already holds (batch loops pass a shared ctx)."""
    ctx = ctx or merchant_context(client, str(card["merchant_id"]))
    now = now or datetime.now(UTC)
    return build_pass_content(
        card, program, merchant, customer,
        messages=_card_messages(client, str(card["id"]), now),
        merchant_locations=ctx.class_locations,
        card_locations=_card_locations(client, card, ctx),
        balance_expires_on=_balance_expires_on(client, card, program, ctx),
    )


def program_content(client, program: dict, merchant: dict | None,
                    ctx: MerchantContext | None = None) -> PassContent:
    """Class-level content. The class payload reads only program/merchant facts, so a
    placeholder card stands in — the program may not have issued a card yet."""
    ctx = ctx or merchant_context(client, str(program["merchant_id"]))
    placeholder = {"id": "class", "merchant_id": program["merchant_id"],
                   "opaque_token": "-", "language": None}
    return build_pass_content(placeholder, program, merchant, None,
                              merchant_locations=ctx.class_locations)


def _one(client, table: str, row_id) -> dict | None:
    if not row_id:
        return None
    rows = _db(client).table(table).select("*").eq("id", str(row_id)).limit(1).execute().data
    return rows[0] if rows else None


def load_card_content(client, card_id: str) -> PassContent | None:
    card = _one(client, "cards", card_id)
    if card is None:
        return None
    program = _one(client, "programs", card["program_id"])
    if program is None:
        return None
    return content_for(client, card, program, _one(client, "merchants", card["merchant_id"]),
                       _one(client, "customers", card.get("customer_id")))


def push_card(client, card_id: str) -> None:
    content = load_card_content(client, card_id)
    if content is not None:
        get_wallet_provider().update(content)


def issue_card(client, card_id: str) -> IssuedPass | None:
    content = load_card_content(client, card_id)
    return get_wallet_provider().issue(content) if content is not None else None


def card_add_link(client, card_id: str) -> str | None:
    content = load_card_content(client, card_id)
    return get_wallet_provider().add_link(content) if content is not None else None


def send_card_message(client, card_id: str, message: PassMessage, *,
                      notify: bool = True) -> None:
    """addMessage needs only the object id and the card's language, so this reads one
    row — the dispatcher calls it once per recipient."""
    rows = (_db(client).table("cards").select("id,program_id,merchant_id,opaque_token,language")
            .eq("id", card_id).limit(1).execute().data or [])
    if not rows:
        raise LookupError(f"card {card_id} not found")
    c = rows[0]
    content = PassContent(
        card_id=str(c["id"]), program_id=str(c["program_id"]),
        merchant_id=str(c["merchant_id"]), program_type="", program_name="",
        merchant_name="", holder_name=None, opaque_token=c["opaque_token"],
        language=c.get("language") or "es")
    get_wallet_provider().send_message(content, message, notify=notify)


def sync_program(client, program_id: str, ctx: MerchantContext | None = None) -> None:
    program = _one(client, "programs", program_id)
    if program is None:
        return
    merchant = _one(client, "merchants", program["merchant_id"])
    get_wallet_provider().sync_program(program_content(client, program, merchant, ctx))


def sync_merchant_classes(client, merchant_id: str) -> None:
    """Locales or plan changed → every program class gets the new merchantLocations.
    Best-effort per program; logs and carries on."""
    try:
        ctx = merchant_context(client, merchant_id)
        programs = (_db(client).table("programs").select("id").eq("merchant_id", merchant_id)
                    .execute().data or [])
    except Exception:  # noqa: BLE001 - wallet reflection is non-critical
        log.warning("class sync setup failed for merchant %s", merchant_id, exc_info=True)
        return
    for p in programs:
        try:
            sync_program(client, p["id"], ctx)
        except Exception:  # noqa: BLE001
            log.warning("class sync failed for program %s", p["id"], exc_info=True)


def _parse_ts(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
