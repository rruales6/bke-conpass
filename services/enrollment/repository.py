"""Persistence for the enrollment service (Data API via secret key)."""
from __future__ import annotations

from conpass_common.db import service_client


class EnrollmentRepository:
    def __init__(self, client=None):
        self._c = client or service_client()

    def get_program(self, program_id: str) -> dict | None:
        rows = self._c.table("programs").select("*").eq("id", program_id).execute().data
        return rows[0] if rows else None

    def find_card_by_dedupe(self, program_id: str, dedupe_key: str) -> dict | None:
        rows = self._c.table("cards").select("*").eq(
            "program_id", program_id).eq("dedupe_key", dedupe_key).execute().data
        return rows[0] if rows else None

    def create_customer(self, data: dict) -> dict:
        return self._c.table("customers").insert(data).execute().data[0]

    def create_card(self, data: dict) -> dict:
        return self._c.table("cards").insert(data).execute().data[0]
