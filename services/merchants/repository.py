"""Persistence for the merchants service (Data API via secret key)."""
from __future__ import annotations

from conpass_common.db import service_client


class MerchantsRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    # --- merchant + subscription ---
    def create_merchant(self, data: dict) -> dict:
        return self._c.table("merchants").insert(data).execute().data[0]

    def create_subscription(self, merchant_id: str, tier: str) -> dict:
        self._c.table("subscriptions").insert(
            {"merchant_id": merchant_id, "tier": tier}).execute()
        return self.get_subscription(merchant_id)

    def get_subscription(self, merchant_id: str) -> dict | None:
        rows = self._c.table("subscriptions").select("*").eq(
            "merchant_id", merchant_id).execute().data
        return rows[0] if rows else None

    def get_merchant(self, merchant_id: str) -> dict | None:
        rows = self._c.table("merchants").select("*").eq(
            "id", merchant_id).execute().data
        return rows[0] if rows else None

    def delete_merchant(self, merchant_id: str) -> None:
        self._c.table("merchants").delete().eq("id", merchant_id).execute()

    # --- demo sandbox (public GET /demo) ---
    def latest_demo_merchant(self) -> dict | None:
        rows = (self._c.table("merchants").select("*").eq("is_demo", True)
                .order("created_at", desc=True).limit(1).execute().data)
        return rows[0] if rows else None

    def latest_program(self, merchant_id: str) -> dict | None:
        rows = (self._c.table("programs").select("*").eq("merchant_id", merchant_id)
                .eq("active", True).order("created_at", desc=True).limit(1).execute().data)
        return rows[0] if rows else None

    def owner_profile(self, merchant_id: str) -> dict | None:
        rows = (self._c.table("profiles").select("*").eq("merchant_id", merchant_id)
                .eq("role", "merchant_owner").limit(1).execute().data)
        return rows[0] if rows else None

    # --- operation users ---
    def list_operation_users(self, merchant_id: str) -> list[dict]:
        return self._c.table("profiles").select("*").eq(
            "merchant_id", merchant_id).eq("role", "operation_user").execute().data

    def count_operation_users(self, merchant_id: str) -> int:
        res = self._c.table("profiles").select("user_id", count="exact").eq(
            "merchant_id", merchant_id).eq("role", "operation_user").execute()
        return res.count or 0
