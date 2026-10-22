"""Persistence for the merchants service (Data API via secret key)."""
from __future__ import annotations

from conpass_common.db import service_client
from conpass_common.demo import DEMO_CUSTOMER_EMAIL


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

    def sample_card_code(self, merchant_id: str, program_id: str) -> str | None:
        """Opaque token of the seeded demo customer's active card: the one in
        `program_id` if there is one, else their most recent in any of the merchant's
        programs. Read-only."""
        customers = (self._c.table("customers").select("id").eq("merchant_id", merchant_id)
                     .eq("email", DEMO_CUSTOMER_EMAIL).limit(1).execute().data)
        if not customers:
            return None
        cards = (self._c.table("cards").select("program_id,opaque_token,created_at")
                 .eq("merchant_id", merchant_id).eq("customer_id", customers[0]["id"])
                 .eq("active", True).order("created_at", desc=True).execute().data)
        if not cards:
            return None
        preferred = next((r for r in cards if r["program_id"] == program_id), cards[0])
        return preferred["opaque_token"]

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

    # --- plan usage (GET /merchants/{id}/usage) ---
    def active_card_count(self, merchant_id: str) -> int:
        rows = self._c.table("merchant_active_passes").select("*").eq(
            "merchant_id", merchant_id).execute().data
        return rows[0]["active_pass_count"] if rows else 0

    def count_programs(self, merchant_id: str) -> int:
        res = self._c.table("programs").select("id", count="exact").eq(
            "merchant_id", merchant_id).execute()
        return res.count or 0
