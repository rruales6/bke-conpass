"""Persistence for the enrollment service (Data API via secret key)."""
from __future__ import annotations

from conpass_common.db import service_client


class EnrollmentRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    def get_program(self, program_id: str) -> dict | None:
        rows = self._c.table("programs").select("*").eq("id", program_id).execute().data
        return rows[0] if rows else None

    def get_merchant(self, merchant_id: str) -> dict | None:
        rows = self._c.table("merchants").select("*").eq("id", merchant_id).execute().data
        return rows[0] if rows else None

    def birthday_offer(self, merchant_id: str) -> bool:
        """Pro (automated_campaigns) and the birthday automation switched on."""
        from conpass_common.plans import AUTOMATED_CAMPAIGNS, get_plan
        subs = (self._c.table("subscriptions").select("tier")
                .eq("merchant_id", merchant_id).execute().data or [])
        if not subs or not get_plan(subs[0]["tier"]).has(AUTOMATED_CAMPAIGNS):
            return False
        rows = (self._c.table("merchant_notification_settings").select("birthday_on")
                .eq("merchant_id", merchant_id).execute().data or [])
        return bool(rows and rows[0].get("birthday_on"))

    def get_customer(self, customer_id: str) -> dict | None:
        rows = self._c.table("customers").select("*").eq("id", customer_id).execute().data
        return rows[0] if rows else None

    def find_card_by_dedupe(self, program_id: str, dedupe_key: str) -> dict | None:
        rows = self._c.table("cards").select("*").eq(
            "program_id", program_id).eq("dedupe_key", dedupe_key).execute().data
        return rows[0] if rows else None

    def get_card(self, card_id: str) -> dict | None:
        rows = self._c.table("cards").select("*").eq("id", card_id).execute().data
        return rows[0] if rows else None

    def update_customer(self, customer_id: str, patch: dict) -> None:
        self._c.table("customers").update(patch).eq("id", customer_id).execute()

    def link_customer(self, card_id: str, customer_id: str) -> None:
        self._c.table("cards").update({"customer_id": customer_id}).eq(
            "id", card_id).execute()

    def create_customer(self, data: dict) -> dict:
        return self._c.table("customers").insert(data).execute().data[0]

    def create_card(self, data: dict) -> dict:
        return self._c.table("cards").insert(data).execute().data[0]
