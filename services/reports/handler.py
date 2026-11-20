"""reports service: second visit, weekly cohorts, per campaign / level / branch, CSV export
(Phase 18, D21). Read-only. The counting is SQL (db/migrations/0018_reports.sql); the
definitions are documented there and in `logic`.

Who sees what:
- merchant_owner: their merchant, every branch.
- platform_admin: `merchantId` is required.
- operation user: only a manager (Pro), limited to the branches they are assigned
  (`staff.manager_scope`); a cashier is refused.
- Without a manager scope, filtering by branch and the `branches` block need the
  `branch_reports` feature (Pro).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.campaigns import SEASONAL, today_ec
from conpass_common.errors import Forbidden, ValidationFailed
from conpass_common.plans import BRANCH_REPORTS, DATA_EXPORT, get_plan, require_feature
from conpass_common.staff import manager_scope
from fastapi import Response

from . import logic
from .repository import ReportsRepository

app = create_app(service="reports")

VALID_WEEKS = (4, 8, 12)
Section = Literal["cohorts", "campaigns", "levels", "branches", "redemptions"]


def _weeks(weeks: int) -> int:
    if weeks not in VALID_WEEKS:
        raise ValidationFailed("weeks must be 4, 8 or 12", details={"reason": "invalid_weeks"})
    return weeks


def get_repo() -> ReportsRepository:
    return ReportsRepository()


@dataclass
class Scope:
    merchant_id: str
    locations: list[str] | None      # a manager's branches (None = the whole merchant)
    managed: bool                    # True when the caller is a manager


def _resolve(repo: ReportsRepository, identity, merchant_id: UUID | None) -> Scope:
    wanted = str(merchant_id) if merchant_id else None
    if "platform_admin" in identity.roles:
        if wanted is None:
            raise ValidationFailed("merchantId is required for a platform admin",
                                   details={"reason": "merchant_required"})
        return Scope(wanted, None, False)
    if not identity.merchant_id:
        raise Forbidden("no merchant in scope")
    if wanted is not None and wanted != identity.merchant_id:
        raise Forbidden("cross-tenant access denied")
    if "merchant_owner" in identity.roles:
        return Scope(identity.merchant_id, None, False)
    # An operation user: a manager gets their branches, a cashier gets Forbidden.
    return Scope(identity.merchant_id, manager_scope(repo.client, identity), True)


def _filters(repo: ReportsRepository, scope: Scope, location_id: UUID | None,
             tier: str | None = None) -> tuple[list[str] | None, bool]:
    """(locations to filter by, whether the branches block may be shown)."""
    loc = str(location_id) if location_id else None
    if scope.managed:
        assert scope.locations is not None
        if loc is not None:
            if loc not in scope.locations:
                raise Forbidden("that branch is outside your scope")
            return [loc], True
        return list(scope.locations), True
    tier = tier or repo.tier(scope.merchant_id)
    if loc is not None:
        require_feature(tier, BRANCH_REPORTS)
        return [loc], True
    return None, get_plan(tier).has(BRANCH_REPORTS)


def _overview(repo: ReportsRepository, scope: Scope, program_id: UUID | None,
              location_id: UUID | None, weeks: int, today, tier: str | None = None) -> dict:
    locations, with_branches = _filters(repo, scope, location_id, tier)
    program = str(program_id) if program_id else None
    m = scope.merchant_id
    starts = logic.period_weeks(today, weeks)
    start = starts[0]

    cohorts = logic.build_cohorts(
        repo.cohort_weeks(m, program, locations, start, today), starts, today)
    new = sum(c["newCustomers"] for c in cohorts)
    returned = sum(c["returned"] for c in cohorts)
    visits, redemptions = repo.activity(m, program, locations, start, today)

    campaigns = []
    for r in repo.campaigns(m, program, locations, start, today):
        campaigns.append({
            "programId": r["program_id"], "name": r["name"], "type": r["type"],
            "visits": int(r["visits"]), "redemptions": int(r["redemptions"]),
            "secondVisitRate": (None if r["type"] == SEASONAL else
                                logic.rate(int(r["returned"]), int(r["new_customers"]))),
        })

    out = {
        "periodStart": start.isoformat(), "periodEnd": today.isoformat(), "weeks": weeks,
        "kpis": {"secondVisit": logic.second_visit(new, returned), "newCustomers": new,
                 "visits": visits, "redemptions": redemptions},
        "cohorts": cohorts,
        "campaigns": campaigns,
        "levels": [{"level": int(r["level"]), "count": int(r["count"])}
                   for r in repo.levels(m, program, locations)],
    }
    if with_branches:
        out["branches"] = [
            {"locationId": r["location_id"], "name": r["name"],
             "newCustomers": int(r["new_customers"]), "visits": int(r["visits"]),
             "secondVisitRate": logic.rate(int(r["returned"]), int(r["new_customers"]))}
            for r in repo.branches(m, program, locations, start, today)]
    return out


@app.get("/reports/summary")
def report_summary(identity: CurrentIdentity, merchantId: UUID | None = None):
    repo = get_repo()
    scope = _resolve(repo, identity, merchantId)
    m, locations, today = scope.merchant_id, scope.locations, today_ec()
    new, returned = repo.second_visit(m, None, locations, today - timedelta(days=29), today)
    previous = repo.second_visit(m, None, locations, today - timedelta(days=59),
                                 today - timedelta(days=30))
    _, redemptions = repo.activity(m, None, locations, today.replace(day=1), today)
    return {
        "secondVisit": logic.second_visit(new, returned, previous),
        "redemptionsThisMonth": redemptions,
        "programs": [{"programId": r["program_id"], "activeCards": int(r["active_cards"]),
                      "redemptions30d": int(r["redemptions_30d"]),
                      "newCards7d": int(r["new_cards_7d"])}
                     for r in repo.program_figures(m, locations, today)],
    }


@app.get("/reports/overview")
def report_overview(identity: CurrentIdentity, merchantId: UUID | None = None,
                    weeks: int = 8, programId: UUID | None = None,
                    locationId: UUID | None = None):
    weeks = _weeks(weeks)
    repo = get_repo()
    scope = _resolve(repo, identity, merchantId)
    return _overview(repo, scope, programId, locationId, weeks, today_ec())


@app.get("/reports/export")
def report_export(section: Section, identity: CurrentIdentity,
                  merchantId: UUID | None = None, weeks: int = 8,
                  programId: UUID | None = None, locationId: UUID | None = None):
    weeks = _weeks(weeks)
    repo = get_repo()
    scope = _resolve(repo, identity, merchantId)
    tier = repo.tier(scope.merchant_id)
    require_feature(tier, DATA_EXPORT)
    today = today_ec()
    if section == "redemptions":
        locations, _ = _filters(repo, scope, locationId, tier)
        start = logic.period_weeks(today, weeks)[0]
        data = {"redemptions": repo.redemptions(
            scope.merchant_id, str(programId) if programId else None, locations, start, today)}
    else:
        data = _overview(repo, scope, programId, locationId, weeks, today, tier)
        if section == "branches" and "branches" not in data:
            require_feature(tier, BRANCH_REPORTS)
    return Response(
        content=logic.section_csv(section, data), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="conpass-{section}-{today.isoformat()}.csv"'})


handler = lambda_handler(app)
