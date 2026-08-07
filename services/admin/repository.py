"""Persistence for the admin service (Data API via secret key).

platform_admin reads are cross-tenant by design — the secret-key client bypasses RLS
and no merchant filter is applied here (tenant scoping is enforced only where identity
requires it, which admin does not).
"""
from __future__ import annotations

from conpass_common.db import service_client


class AdminRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    def list_merchants(self) -> list[dict]:
        return (self._c.table("merchants").select("*")
                .order("created_at", desc=True).execute().data)

    def list_subscriptions(self) -> list[dict]:
        return self._c.table("subscriptions").select("*").execute().data

    def get_subscription(self, merchant_id: str) -> dict | None:
        rows = self._c.table("subscriptions").select("*").eq(
            "merchant_id", merchant_id).execute().data
        return rows[0] if rows else None

    def update_subscription(self, merchant_id: str, patch: dict) -> dict:
        return self._c.table("subscriptions").update(patch).eq(
            "merchant_id", merchant_id).execute().data[0]

    def active_pass_count(self, merchant_id: str) -> int:
        rows = self._c.table("merchant_active_passes").select("*").eq(
            "merchant_id", merchant_id).execute().data
        return rows[0]["active_pass_count"] if rows else 0

    # --- platform payment settings (no admin-side GET: the public /payment-settings
    # endpoint already serves the same shape and needs no auth, so the console reads
    # through that instead of duplicating a fetch here) ---
    def update_payment_settings(self, patch: dict) -> dict:
        return (self._c.table("platform_payment_settings").update(patch)
                .eq("id", True).execute().data[0])
