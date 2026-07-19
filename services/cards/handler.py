"""cards service — read a card & (re)generate wallet links."""
from __future__ import annotations

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.errors import NotFound
from conpass_common.providers import get_wallet_provider
from conpass_common.providers.wallet import build_pass_content

app = create_app(service="cards")


def _client():
    return service_client()


def _card_model(c: dict) -> dict:
    return {
        "id": c["id"], "programId": c["program_id"], "merchantId": c["merchant_id"],
        "customerId": c.get("customer_id"), "type": c["type"],
        "opaqueToken": c["opaque_token"],
        "balance": {
            "mechanic": "points" if c["type"] == "loyalty_points" else "stamps",
            "stamps": c.get("stamps", 0), "points": c.get("points", 0),
            "rewardsAvailable": c.get("rewards_available", 0),
            "membershipActiveUntil": c.get("membership_active_until"),
        },
        "holderName": c.get("holder_name"),
        "active": c.get("active", True),
        "walletInstalled": c.get("wallet_installed", False),
        "createdAt": c["created_at"],
    }


@app.get("/cards/{card_id}")
def get_card(card_id: str, identity: CurrentIdentity):
    rows = _client().table("cards").select("*").eq("id", card_id).execute().data
    if not rows:
        raise NotFound("card not found")
    card = rows[0]
    identity.require_merchant(card["merchant_id"])
    return _card_model(card)


@app.get("/cards/{card_id}/wallet-links")
def get_wallet_links(card_id: str, identity: CurrentIdentity):
    # Uses WalletProvider.add_link (Google now, Apple later). NotConfigured → 503.
    client = _client()
    cards = client.table("cards").select("*").eq("id", card_id).execute().data
    if not cards:
        raise NotFound("card not found")
    card = cards[0]
    identity.require_merchant(card["merchant_id"])

    programs = client.table("programs").select("*").eq(
        "id", card["program_id"]).execute().data
    if not programs:
        raise NotFound("program not found")
    merchants = client.table("merchants").select("*").eq(
        "id", card["merchant_id"]).execute().data

    content = build_pass_content(card, programs[0], merchants[0] if merchants else None)
    return {"google": get_wallet_provider().add_link(content)}


handler = lambda_handler(app)
