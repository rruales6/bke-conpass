#!/usr/bin/env python3
"""Idempotently seed the public demo tenant (Phase 5 /demo sandbox).

Creates (or repairs) one demo merchant flagged `is_demo=true`, its owner Supabase Auth
login (fixed shared password), the two pinned demo campaigns (stamps + points,
`demo_pinned=true`, never purged or edited; visitor-made campaigns are deleted after 30
minutes by the notifier) and a sample customer + card (pre-fills the demo cashier). Safe
to re-run: it keys on the fixed demo owner email, so a second run just re-asserts the password/flag.

Usage:  python scripts/seed_demo.py     (needs Supabase secrets)
"""
from __future__ import annotations

import sys

sys.path.insert(0, "layers/common/python")

from conpass_common.db import service_client  # noqa: E402
from conpass_common.demo import (  # noqa: E402
    DEMO_BUSINESS_NAME,
    DEMO_COLOR,
    DEMO_CUSTOMER_EMAIL,
    DEMO_OWNER_EMAIL,
    DEMO_PASSWORD,
    DEMO_PINNED_POINTS_NAME,
    DEMO_PINNED_STAMPS_NAME,
    DEMO_REWARD,
    DEMO_STAMPS_FOR_REWARD,
    ensure_sample_card,
)
from conpass_common.provisioning import provision_user  # noqa: E402

PINNED_CAMPAIGNS = (
    {"type": "loyalty_stamps", "name": DEMO_PINNED_STAMPS_NAME, "mechanic": "stamps",
     "stamps_for_reward": DEMO_STAMPS_FOR_REWARD, "reward": DEMO_REWARD, "welcome_bonus": 1},
    {"type": "loyalty_points", "name": DEMO_PINNED_POINTS_NAME, "mechanic": "points",
     "points_for_reward": 100, "points_per_dollar": 10,
     "reward": "Un postre gratis con 100 puntos", "welcome_bonus": 0},
)


def _owner_profile(c) -> dict | None:
    rows = (c.table("profiles").select("*").eq("email", DEMO_OWNER_EMAIL)
            .eq("role", "merchant_owner").limit(1).execute().data)
    return rows[0] if rows else None


def main() -> None:
    c = service_client()
    profile = _owner_profile(c)

    if profile:
        merchant_id = profile["merchant_id"]
        c.table("merchants").update({"is_demo": True}).eq("id", merchant_id).execute()
        c.auth.admin.update_user_by_id(profile["user_id"], {"password": DEMO_PASSWORD})
        print(f"↻ reused demo merchant {merchant_id}")
    else:
        merchant = c.table("merchants").insert({
            "business_name": DEMO_BUSINESS_NAME, "category": "cafe", "city": "Quito",
            "contact_name": "Demo Owner", "contact_email": DEMO_OWNER_EMAIL,
            "is_demo": True,
        }).execute().data[0]
        merchant_id = merchant["id"]
        c.table("subscriptions").insert({"merchant_id": merchant_id, "tier": "growth"}).execute()
        owner = provision_user(email=DEMO_OWNER_EMAIL, roles=["merchant_owner"],
                               merchant_id=merchant_id, name="Demo Owner")
        c.auth.admin.update_user_by_id(owner.user_id, {"password": DEMO_PASSWORD})
        print(f"＋ created demo merchant {merchant_id}")

    for spec in PINNED_CAMPAIGNS:
        rows = (c.table("programs").select("id,demo_pinned").eq("merchant_id", merchant_id)
                .eq("name", spec["name"]).limit(1).execute().data)
        if rows:
            if not rows[0].get("demo_pinned"):
                c.table("programs").update({"demo_pinned": True, "active": True}).eq(
                    "id", rows[0]["id"]).execute()
            print(f"✓ pinned demo campaign exists: {spec['name']} ({rows[0]['id']})")
        else:
            prog = c.table("programs").insert({
                "merchant_id": merchant_id, "color": DEMO_COLOR, "wallets": ["google"],
                "active": True, "demo_pinned": True, **spec,
            }).execute().data[0]
            print(f"＋ created pinned demo campaign {spec['name']} ({prog['id']})")

    card = ensure_sample_card(c, merchant_id)
    if card:
        print(f"✓ sample card ready for {DEMO_CUSTOMER_EMAIL}: {card['id']}")

    print(f"\ndemo ready → owner={DEMO_OWNER_EMAIL}  password={DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
