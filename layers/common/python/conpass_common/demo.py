"""Public self-serve demo sandbox constants (Phase 5).

One canonical demo tenant, seeded idempotently by scripts/seed_demo.py and discovered at
runtime by GET /demo. The password is a *shared, intentionally-public* sandbox credential:
the demo login only ever sees demo-flagged data (tenant RLS + merchant_id scoping), so
exposing it is safe — and is the point (visitors get instant, frictionless access).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .campaigns import ECUADOR_TZ
from .cards import new_opaque_token

log = logging.getLogger(__name__)

DEMO_OWNER_EMAIL = "demo-owner@conpass.cards"
DEMO_PASSWORD = "conpass-demo-2026"          # shared sandbox password (public by design)
DEMO_BUSINESS_NAME = "Café Vecino (Demo)"
DEMO_PROGRAM_NAME = "Club Café Vecino"
DEMO_REWARD = "La 8.ª bebida es gratis"
DEMO_STAMPS_FOR_REWARD = 8
DEMO_COLOR = "#2F6B4F"
# The demo's pre-seeded customer + card, so the cashier can be pre-filled with a real code.
DEMO_CUSTOMER_NAME = "Cliente Demo"
DEMO_CUSTOMER_EMAIL = "demo-customer@conpass.cards"
DEMO_CARD_DEDUPE_KEY = "demo-sample-card"   # makes the seeded card unique per program
# Anti-spam rules for the public demo (anyone can use the shared credentials).
DEMO_CARD_TTL_DAYS = 7              # demo cards are deactivated this long after enrolment
DEMO_DAILY_MANUAL_SENDS = 3         # manual notifications per Ecuador day


def is_demo_merchant(client, merchant_id: str) -> bool:
    """True when `merchants.is_demo` is set; False for a missing merchant."""
    rows = (client.table("merchants").select("is_demo").eq("id", str(merchant_id))
            .limit(1).execute().data or [])
    return bool(rows and rows[0].get("is_demo"))


def manual_sends_today(client, merchant_id: str, now: datetime | None = None) -> int:
    """Manual notifications the merchant created during the current Ecuador day, in any
    status (queued, scheduled, sent, cancelled)."""
    now = now or datetime.now(UTC)
    start = now.astimezone(ECUADOR_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    res = (client.table("notifications").select("id", count="exact")
           .eq("merchant_id", str(merchant_id)).eq("source", "manual")
           .gte("created_at", start.astimezone(UTC).isoformat())
           .lt("created_at", end.astimezone(UTC).isoformat()).execute())
    return res.count if res.count is not None else len(res.data or [])


def _revoke_pass(card_id: str) -> None:
    from .config import settings
    from .providers import get_wallet_provider

    issuer = settings.google_wallet_issuer_id
    if not issuer:
        return
    get_wallet_provider().revoke(f"{issuer}.card_{card_id}")


def expire_demo_cards(client, now: datetime | None = None, *, revoke=None) -> dict:
    """Deactivate demo-merchant cards enrolled more than DEMO_CARD_TTL_DAYS ago and revoke
    their Google pass best-effort (any wallet error is logged and skipped). The seeded
    sample card (dedupe key DEMO_CARD_DEDUPE_KEY) is never touched: the public demo cashier
    pre-fills it."""
    revoke = revoke or _revoke_pass
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=DEMO_CARD_TTL_DAYS)).astimezone(UTC).isoformat()
    merchants = (client.table("merchants").select("id").eq("is_demo", True)
                 .execute().data or [])
    out = {"deactivated": 0, "revoke_failed": 0}
    for m in merchants:
        cards = (client.table("cards").select("id,dedupe_key")
                 .eq("merchant_id", m["id"]).eq("active", True)
                 .lt("created_at", cutoff).execute().data or [])
        for card in cards:
            if card.get("dedupe_key") == DEMO_CARD_DEDUPE_KEY:
                continue
            client.table("cards").update({"active": False}).eq("id", card["id"]).execute()
            out["deactivated"] += 1
            try:
                revoke(card["id"])
            except Exception:  # noqa: BLE001 - best-effort: a 404/any wallet error is skipped
                out["revoke_failed"] += 1
                log.warning("could not revoke the pass of demo card %s", card["id"],
                            exc_info=True)
    return out


def ensure_sample_card(c, merchant_id: str) -> dict | None:
    """Idempotently make sure the demo merchant has its sample customer + an active card
    in its latest active program; returns that card row (None if there is no program).

    Backs the public GET /demo `sampleCardCode` so the demo cashier can be pre-filled.
    No Google Wallet object is issued — the card exists only to be looked up by its
    opaque token. `c` is a Supabase Data API client. Re-running creates no duplicates:
    the customer is keyed on (merchant, email) and the card on (program, dedupe key).
    """
    programs = (c.table("programs").select("*").eq("merchant_id", merchant_id)
                .eq("active", True).order("created_at", desc=True).limit(1).execute().data)
    if not programs:
        return None
    program = programs[0]

    customers = (c.table("customers").select("*").eq("merchant_id", merchant_id)
                 .eq("email", DEMO_CUSTOMER_EMAIL).limit(1).execute().data)
    customer = customers[0] if customers else c.table("customers").insert({
        "merchant_id": merchant_id, "full_name": DEMO_CUSTOMER_NAME,
        "email": DEMO_CUSTOMER_EMAIL, "consent": True,
    }).execute().data[0]

    cards = (c.table("cards").select("*").eq("program_id", program["id"])
             .eq("dedupe_key", DEMO_CARD_DEDUPE_KEY).limit(1).execute().data)
    if cards:
        card = cards[0]
        if not card.get("active", True) or card.get("customer_id") != customer["id"]:
            card = c.table("cards").update(
                {"active": True, "customer_id": customer["id"]},
            ).eq("id", card["id"]).execute().data[0]
        return card

    welcome = program.get("welcome_bonus") or 0
    mechanic = program.get("mechanic")
    return c.table("cards").insert({
        "program_id": program["id"],
        "merchant_id": merchant_id,
        "customer_id": customer["id"],
        "type": program["type"],
        "opaque_token": new_opaque_token(),
        "stamps": welcome if mechanic == "stamps" else 0,
        "points": welcome if mechanic == "points" else 0,
        "holder_name": "Cliente D.",
        "dedupe_key": DEMO_CARD_DEDUPE_KEY,
        "language": "es",
    }).execute().data[0]
