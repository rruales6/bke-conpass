"""Persistence for the merchants service (Data API via secret key)."""
from __future__ import annotations

from conpass_common.db import service_client


class MerchantsRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    # --- merchant + subscription ---
    def create_merchant(self, data: dict) -> dict:
        return self._c.table("merchants").insert(data).execute().data[0]

    def create_subscription(
        self, merchant_id: str, tier: str, *,
        payment_proof_key: str | None = None,
        payment_proof_uploaded_at: str | None = None,
    ) -> dict:
        data = {"merchant_id": merchant_id, "tier": tier}
        if payment_proof_key is not None:
            data["payment_proof_key"] = payment_proof_key
            data["payment_proof_uploaded_at"] = payment_proof_uploaded_at
        self._c.table("subscriptions").insert(data).execute()
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

    # --- platform payment settings (public GET /payment-settings) ---
    def get_payment_settings(self) -> dict | None:
        rows = (self._c.table("platform_payment_settings").select("*")
                .eq("id", True).limit(1).execute().data)
        return rows[0] if rows else None

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

    def get_operation_user(self, merchant_id: str, user_id: str) -> dict | None:
        rows = (self._c.table("profiles").select("*").eq("user_id", user_id)
                .eq("merchant_id", merchant_id).eq("role", "operation_user")
                .limit(1).execute().data)
        return rows[0] if rows else None

    def delete_operation_user_profile(self, merchant_id: str, user_id: str) -> None:
        # profiles.user_id is a bare PK (no FK to auth.users), so removing the auth
        # user does not cascade — drop the profile row explicitly.
        (self._c.table("profiles").delete().eq("user_id", user_id)
         .eq("merchant_id", merchant_id).eq("role", "operation_user").execute())

    def count_operation_users(self, merchant_id: str) -> int:
        res = self._c.table("profiles").select("user_id", count="exact").eq(
            "merchant_id", merchant_id).eq("role", "operation_user").execute()
        return res.count or 0
