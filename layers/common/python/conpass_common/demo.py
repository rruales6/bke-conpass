"""Public self-serve demo sandbox constants (Phase 5).

One canonical demo tenant, seeded idempotently by scripts/seed_demo.py and discovered at
runtime by GET /demo. The password is a *shared, intentionally-public* sandbox credential:
the demo login only ever sees demo-flagged data (tenant RLS + merchant_id scoping), so
exposing it is safe — and is the point (visitors get instant, frictionless access).
"""
from __future__ import annotations

from .cards import new_opaque_token

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
