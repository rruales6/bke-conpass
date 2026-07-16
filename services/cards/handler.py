"""cards service — read a card & (re)generate wallet links."""
from __future__ import annotations

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.errors import NotFound, NotImplementedYet

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
    # Uses WalletProvider.add_link (Google now, Apple later).
    raise NotImplementedYet("Phase 4")


handler = lambda_handler(app)
