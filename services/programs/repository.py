"""Persistence for the programs service (Data API via secret key)."""
from __future__ import annotations

from conpass_common.db import service_client


class ProgramsRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    def count_programs(self, merchant_id: str) -> int:
        res = self._c.table("programs").select("id", count="exact").eq(
            "merchant_id", merchant_id).execute()
        return res.count or 0

    def program_limit(self, merchant_id: str) -> int | None:
        rows = self._c.table("subscriptions").select("program_limit").eq(
            "merchant_id", merchant_id).execute().data
        return rows[0]["program_limit"] if rows else None

    def create(self, data: dict) -> dict:
        return self._c.table("programs").insert(data).execute().data[0]

    def list(self, merchant_id: str) -> list[dict]:
        return self._c.table("programs").select("*").eq(
            "merchant_id", merchant_id).order("created_at").execute().data

    def get(self, program_id: str) -> dict | None:
        rows = self._c.table("programs").select("*").eq("id", program_id).execute().data
        return rows[0] if rows else None

    def update(self, program_id: str, patch: dict) -> dict:
        return self._c.table("programs").update(patch).eq(
            "id", program_id).execute().data[0]

    def list_card_rows(self, program_id: str, limit: int) -> list[dict]:
        """Cards of a program, for reflecting an appearance change into installed passes."""
        return self._c.table("cards").select("*").eq(
            "program_id", program_id).limit(limit).execute().data

    def count_cards(self, program_id: str) -> int:
        res = self._c.table("cards").select("id", count="exact").eq(
            "program_id", program_id).execute()
        return res.count or 0

    def get_merchant(self, merchant_id: str) -> dict | None:
        rows = self._c.table("merchants").select("*").eq(
            "id", merchant_id).execute().data
        return rows[0] if rows else None

    def get_customers_by_ids(self, customer_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch customers for a wallet push — one query for up to
        WALLET_PUSH_MAX_CARDS cards, not one query per card."""
        if not customer_ids:
            return {}
        rows = self._c.table("customers").select("*").in_(
            "id", customer_ids).execute().data
        return {r["id"]: r for r in rows}
