"""Persistence for the reports service: thin wrappers over the 0018 SQL functions."""
from __future__ import annotations

from datetime import date

from conpass_common.db import service_client
from conpass_common.plans import tier_for_merchant

# PostgREST caps a response at 1000 rows by default; the redemptions export pages through.
PAGE = 1000
MAX_PAGES = 20


class ReportsRepository:
    def __init__(self, client=None):
        self.client = client or service_client()

    def _rpc(self, name: str, params: dict) -> list[dict]:
        return self.client.rpc(name, params).execute().data or []

    @staticmethod
    def _filters(merchant_id, program_id, locations, start: date, end: date) -> dict:
        return {"p_merchant": merchant_id, "p_program": program_id,
                "p_locations": locations, "p_from": start.isoformat(),
                "p_to": end.isoformat()}

    def second_visit(self, merchant_id, program_id, locations, start, end) -> tuple[int, int]:
        """(new customers, returned) of the cohort enrolled in [start, end]."""
        rows = self._rpc("report_second_visit",
                         self._filters(merchant_id, program_id, locations, start, end))
        row = rows[0] if rows else {}
        return int(row.get("new_customers") or 0), int(row.get("returned") or 0)

    def cohort_weeks(self, merchant_id, program_id, locations, start, end) -> list[dict]:
        return self._rpc("report_cohort_weeks",
                         self._filters(merchant_id, program_id, locations, start, end))

    def activity(self, merchant_id, program_id, locations, start, end) -> tuple[int, int]:
        """(visits, redemptions)."""
        rows = self._rpc("report_activity",
                         self._filters(merchant_id, program_id, locations, start, end))
        row = rows[0] if rows else {}
        return int(row.get("visits") or 0), int(row.get("redemptions") or 0)

    def campaigns(self, merchant_id, program_id, locations, start, end) -> list[dict]:
        return self._rpc("report_campaigns",
                         self._filters(merchant_id, program_id, locations, start, end))

    def levels(self, merchant_id, program_id, locations) -> list[dict]:
        return self._rpc("report_levels", {"p_merchant": merchant_id,
                                           "p_program": program_id,
                                           "p_locations": locations})

    def branches(self, merchant_id, program_id, locations, start, end) -> list[dict]:
        return self._rpc("report_branches",
                         self._filters(merchant_id, program_id, locations, start, end))

    def program_figures(self, merchant_id, locations, today: date) -> list[dict]:
        return self._rpc("report_program_figures", {
            "p_merchant": merchant_id, "p_locations": locations,
            "p_today": today.isoformat()})

    def redemptions(self, merchant_id, program_id, locations, start, end) -> list[dict]:
        params = self._filters(merchant_id, program_id, locations, start, end)
        out: list[dict] = []
        for page in range(MAX_PAGES):
            lo = page * PAGE
            rows = (self.client.rpc("report_redemptions", params)
                    .range(lo, lo + PAGE - 1).execute().data or [])
            out.extend(rows)
            if len(rows) < PAGE:
                break
        return out

    def tier(self, merchant_id: str) -> str:
        return tier_for_merchant(self.client, merchant_id)
