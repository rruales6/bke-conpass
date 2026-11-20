#!/usr/bin/env python3
"""Idempotently seed three TEST merchants, one per plan (Starter, Growth, Pro), through the real
flow: public signup (POST /merchants with a receipt), then platform-admin activation (POST
/admin/clients/{id}/activate). After that the owner's password is set to a fixed TEST value so the
tier checks (and a human) can sign in.

These are test accounts on the live project, created before there are real customers (owner,
2026-11-19). They are NOT demo accounts: each has its own data. Owner e-mails use the
info@conpass.cards mailbox with plus-addressing so activation mails land in one inbox.

Usage (from backend/, needs secrets):  .venv/bin/python scripts/seed_tier_samples.py
Prints the merchant ids. It never prints passwords; they are the constants below.
"""
from __future__ import annotations

import sys

import httpx

sys.path.insert(0, "layers/common/python")
sys.path.insert(0, ".")

from conpass_common.config import settings  # noqa: E402
from conpass_common.db import service_client  # noqa: E402
from conpass_common.provisioning import complete_password_change  # noqa: E402

from scripts.seed_admin import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402

API = "https://c8glyvxjh7.execute-api.us-east-1.amazonaws.com"

# test credentials — rotate or delete these accounts before going public
SAMPLE_PASSWORD = "Conpass-Prueba-2026"
SAMPLES = {
    "starter": {"businessName": "Prueba Starter · Café Esquina", "city": "Quito",
                "category": "cafe_restaurant", "contactName": "Prueba Starter",
                "contactEmail": "info+starter@conpass.cards", "ruc": "1790000000001"},
    "growth": {"businessName": "Prueba Growth · Barbería Centro", "city": "Quito",
               "category": "beauty", "contactName": "Prueba Growth",
               "contactEmail": "info+growth@conpass.cards", "ruc": "1790000000002"},
    "pro": {"businessName": "Prueba Pro · Panadería Cadena", "city": "Guayaquil",
            "category": "retail", "contactName": "Prueba Pro",
            "contactEmail": "info+pro@conpass.cards", "ruc": "0990000000003"},
}


def _admin_headers(h: httpx.Client) -> dict:
    r = h.post(f"{settings.supabase_url}/auth/v1/token?grant_type=password",
               headers={"apikey": settings.supabase_publishable_key},
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _receipt_key(h: httpx.Client) -> str:
    """Upload a tiny PDF receipt the way the signup page does (presigned PUT)."""
    r = h.post(f"{API}/payment-proofs/upload-url", json={"contentType": "application/pdf"})
    r.raise_for_status()
    up = r.json()
    h.put(up["uploadUrl"], content=b"%PDF-1.4\n% conpass test receipt\n",
          headers={"Content-Type": "application/pdf"}).raise_for_status()
    return up["storageKey"]


def ensure(tier: str, h: httpx.Client, admin: dict) -> str:
    data = SAMPLES[tier]
    c = service_client()
    rows = (c.table("merchants").select("id").eq("business_name", data["businessName"])
            .execute().data)
    if rows:
        mid = rows[0]["id"]
        print(f"↻ {tier}: reusing {mid}")
    else:
        r = h.post(f"{API}/merchants", json={
            **data, "tier": tier, "billingPeriod": "monthly", "language": "es",
            "payment": {"method": "manual_transfer", "proofStorageKey": _receipt_key(h)}})
        r.raise_for_status()
        mid = r.json()["merchant"]["id"]
        print(f"＋ {tier}: signed up {mid} (ownerInviteSent={r.json()['ownerInviteSent']})")
    sub = (c.table("subscriptions").select("tier,payment_status").eq("merchant_id", mid)
           .execute().data[0])
    if sub["tier"] != tier:
        h.patch(f"{API}/admin/clients/{mid}/subscription", headers=admin, json={"tier": tier})
    if sub["payment_status"] != "paid":
        r = h.post(f"{API}/admin/clients/{mid}/activate", headers=admin,
                   json={"rucVerified": True, "proofVerified": True})
        print(f"  activate → {r.status_code}")      # 502 = activated, e-mail failed
    owner = (c.table("profiles").select("user_id").eq("merchant_id", mid)
             .eq("role", "merchant_owner").limit(1).execute().data[0])
    complete_password_change(owner["user_id"], SAMPLE_PASSWORD)
    return mid


def main() -> None:
    with httpx.Client(timeout=60) as h:
        admin = _admin_headers(h)
        for tier in ("starter", "growth", "pro"):
            mid = ensure(tier, h, admin)
            print(f"  {tier} ready → {SAMPLES[tier]['contactEmail']}  merchant={mid}")


if __name__ == "__main__":
    main()
