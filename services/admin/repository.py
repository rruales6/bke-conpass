"""Persistence for the admin service (Data API via secret key).

platform_admin reads are cross-tenant by design — the secret-key client bypasses RLS
and no merchant filter is applied here (tenant scoping is enforced only where identity
requires it, which admin does not).
"""
from __future__ import annotations

from conpass_common.db import service_client
from conpass_common.usage import notifications_used_by_merchant


class AdminRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    def list_merchants(self) -> list[dict]:
        return (self._c.table("merchants").select("*")
                .order("created_at", desc=True).execute().data)

    def get_merchant(self, merchant_id: str) -> dict | None:
        rows = self._c.table("merchants").select("*").eq("id", merchant_id).execute().data
        return rows[0] if rows else None

    def owner_profile(self, merchant_id: str) -> dict | None:
        rows = (self._c.table("profiles").select("*").eq("merchant_id", merchant_id)
                .eq("role", "merchant_owner").limit(1).execute().data)
        return rows[0] if rows else None

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

    def active_card_counts(self) -> dict[str, int]:
        """merchant_id -> active cards, for every merchant in one query."""
        rows = self._c.table("merchant_active_passes").select("*").execute().data
        return {r["merchant_id"]: r["active_pass_count"] for r in rows}

    def active_location_counts(self) -> dict[str, int]:
        """merchant_id -> active branches, for every merchant in one query."""
        counts: dict[str, int] = {}
        rows = (self._c.table("merchant_locations").select("merchant_id")
                .eq("active", True).execute().data or [])
        for r in rows:
            counts[r["merchant_id"]] = counts.get(r["merchant_id"], 0) + 1
        return counts

    def signup_payments(self) -> dict[str, dict]:
        """merchant_id -> the latest signup payment (the receipt to verify), one query."""
        rows = (self._c.table("payments").select("*").eq("kind", "signup")
                .order("created_at", desc=True).execute().data or [])
        latest: dict[str, dict] = {}
        for r in rows:               # newest first: keep the first seen per merchant
            latest.setdefault(r["merchant_id"], r)
        return latest

    def merchants_with_review_pending(self) -> set[str]:
        """Merchants with a plan-change request in pending_review, one query."""
        rows = (self._c.table("plan_change_requests").select("merchant_id")
                .eq("status", "pending_review").execute().data or [])
        return {r["merchant_id"] for r in rows}

    # --- platform settings (singleton, id = true) ---
    def get_platform_settings(self) -> dict | None:
        rows = self._c.table("platform_settings").select("*").eq("id", True).execute().data
        return rows[0] if rows else None

    def update_platform_settings(self, patch: dict) -> dict:
        rows = self._c.table("platform_settings").update(patch).eq("id", True).execute().data
        if rows:
            return rows[0]
        return self._c.table("platform_settings").insert({"id": True, **patch}).execute().data[0]

    def get_payment_settings(self) -> dict | None:
        rows = (self._c.table("platform_payment_settings").select("*")
                .eq("id", True).limit(1).execute().data)
        return rows[0] if rows else None

    # --- platform payment details (read by GET /admin/platform-settings, edited here) ---
    def update_payment_settings(self, patch: dict) -> dict:
        return (self._c.table("platform_payment_settings").update(patch)
                .eq("id", True).execute().data[0])

    def notifications_by_merchant(self, since) -> dict[str, int]:
        """merchant_id -> pushes delivered since `since` (billing overage in estimates)."""
        return notifications_used_by_merchant(self._c, since)

    # --- platform notification rules (singleton, id = true) ---
    def update_notification_rules(self, patch: dict) -> None:
        self._c.table("platform_notification_rules").update(patch).eq("id", True).execute()

    @property
    def client(self):
        return self._c
