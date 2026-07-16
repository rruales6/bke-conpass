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
