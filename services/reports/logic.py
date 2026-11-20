"""Pure report helpers: rounding, Ecuador weeks, the "closed" flag and CSV building.

No I/O here so the definitions can be unit-tested without a database. The SQL in
db/migrations/0018_reports.sql does the counting; this module shapes the numbers.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from conpass_common.campaigns import ECUADOR_TZ

COHORT_WINDOW_DAYS = 30
BOM = "﻿"

SECTIONS = ("cohorts", "campaigns", "levels", "branches", "redemptions")

_TYPE_LABELS = {
    "loyalty_stamps": "Sellos",
    "loyalty_points": "Puntos",
    "seasonal_discount": "Descuento",
    "membership_pass": "Pase de acceso",
}


def rate(returned: int, new: int) -> float | None:
    """returned / new * 100 rounded half-up to one decimal; None when there is no cohort."""
    if not new:
        return None
    pct = Decimal(returned) * 100 / Decimal(new)
    return float(pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def week_start(day: date) -> date:
    """The Monday of the week of `day`."""
    return day - timedelta(days=day.weekday())


def period_weeks(today: date, weeks: int) -> list[date]:
    """Monday of each of the last `weeks` weeks, the current one included, oldest first."""
    current = week_start(today)
    return [current - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


def cohort_closed(start: date, today: date) -> bool:
    """A week is closed once its last customer's 30 days are over."""
    return start + timedelta(days=6 + COHORT_WINDOW_DAYS) < today


def build_cohorts(rows: list[dict], starts: list[date], today: date) -> list[dict]:
    """One CohortWeek per week start (empty weeks are zero), from the SQL's sparse rows."""
    by_week = {str(r["week_start"])[:10]: r for r in rows}
    out = []
    for start in starts:
        row = by_week.get(start.isoformat(), {})
        new, returned = int(row.get("new_customers") or 0), int(row.get("returned") or 0)
        out.append({
            "weekStart": start.isoformat(),
            "newCustomers": new,
            "returned": returned,
            "rate": rate(returned, new),
            "closed": cohort_closed(start, today),
        })
    return out


def second_visit(new: int, returned: int, previous: tuple[int, int] | None = None) -> dict:
    """SecondVisit payload; `previous` is the (new, returned) of the window before."""
    out = {"newCustomers": new, "returned": returned, "rate": rate(returned, new)}
    if previous is not None:
        out["previousRate"] = rate(previous[1], previous[0])
    return out


def ec_datetime(value: str | datetime) -> datetime:
    """A timestamptz from the DB (ISO string) in Ecuador time."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    return dt.astimezone(ECUADOR_TZ)


# --------------------------------------------------------------------------- #
# CSV (UTF-8 with BOM so Excel opens accents correctly; comma, dot decimals)
# --------------------------------------------------------------------------- #
def _cell(value):
    """Text a spreadsheet could read as a formula is neutralised with a leading quote."""
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def build_csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return (BOM + buf.getvalue()).encode("utf-8")


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def section_csv(section: str, data: dict) -> bytes:
    """`data` holds the already-shaped overview blocks (cohorts, campaigns, levels,
    branches) and, for redemptions, the raw SQL rows under "redemptions"."""
    if section == "cohorts":
        return build_csv(
            ["Semana del", "Nuevos", "Volvieron", "Segunda visita (%)", "Estado"],
            [[c["weekStart"], c["newCustomers"], c["returned"], _pct(c["rate"]),
              "Cerrada" if c["closed"] else "En curso"] for c in data["cohorts"]])
    if section == "campaigns":
        return build_csv(
            ["Campaña", "Tipo", "Visitas", "Canjes", "Segunda visita (%)"],
            [[c["name"], _TYPE_LABELS.get(c["type"], c["type"]), c["visits"],
              c["redemptions"], _pct(c.get("secondVisitRate"))] for c in data["campaigns"]])
    if section == "levels":
        return build_csv(["Nivel", "Tarjetas activas"],
                         [[lv["level"], lv["count"]] for lv in data["levels"]])
    if section == "branches":
        return build_csv(
            ["Sucursal", "Nuevos", "Visitas", "Segunda visita (%)"],
            [[b["name"], b["newCustomers"], b["visits"], _pct(b.get("secondVisitRate"))]
             for b in data["branches"]])
    if section == "redemptions":
        return build_csv(
            ["Fecha y hora (Ecuador)", "Cliente", "Campaña", "Sucursal"],
            [[ec_datetime(r["occurred_at"]).strftime("%Y-%m-%d %H:%M"),
              (r.get("customer_name") or "").strip() or "Cliente",
              r.get("program_name") or "", r.get("branch_name") or ""]
             for r in data["redemptions"]])
    raise ValueError(f"unknown section {section!r}")
