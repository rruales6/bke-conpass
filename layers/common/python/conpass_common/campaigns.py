"""Campaign (program) rules shared by every service that renders a Program."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .errors import CampaignNotActive, ValidationFailed

DEFAULT_INACTIVITY_DAYS = 90
ACCUMULATION_TYPES = ("loyalty_stamps", "loyalty_points")


def inactivity_days(program: dict) -> int | None:
    """The abandonment window of an accumulation campaign (stored value, else 90).

    A membership pass has nothing to abandon, so it has no window: None (and the API
    omits the field).
    """
    if program.get("type") not in ACCUMULATION_TYPES:
        return None
    return program.get("inactivity_days") or DEFAULT_INACTIVITY_DAYS


def inactivity_fields(program: dict) -> dict:
    """`{"inactivityDays": n}` for accumulation campaigns, `{}` otherwise."""
    days = inactivity_days(program)
    return {} if days is None else {"inactivityDays": days}


# --------------------------------------------------------------------------- #
# Seasonal campaigns (Phase 14)
# --------------------------------------------------------------------------- #
SEASONAL = "seasonal_discount"

# Ecuador (America/Guayaquil) has no DST: a fixed UTC-5 offset is exact and spares the
# Lambda a zoneinfo/tzdata dependency. No merchant time zone is stored yet, so every
# campaign date is read in Ecuador time.
ECUADOR_TZ = timezone(timedelta(hours=-5))

STATUS_ACTIVE = "active"
STATUS_UPCOMING = "upcoming"
STATUS_ENDED = "ended"
STATUS_DISABLED = "disabled"


def today_ec() -> date:
    """Today's date in Ecuador."""
    return datetime.now(ECUADOR_TZ).date()


def as_date(value: date | datetime | str | None) -> date | None:
    """A DB/API date as a `date` (Supabase returns ISO strings; the models give dates)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def campaign_status(program: dict, today: date | None = None) -> str:
    """disabled / upcoming / ended / active — the one status that gates enrolment and the
    cashier. `valid_from` and `valid_until` are inclusive; the date window only applies
    to seasonal campaigns."""
    if program.get("active", True) is False:
        return STATUS_DISABLED
    if program.get("type") == SEASONAL:
        today = today or today_ec()
        start, end = as_date(program.get("valid_from")), as_date(program.get("valid_until"))
        if start is not None and today < start:
            return STATUS_UPCOMING
        if end is not None and today > end:
            return STATUS_ENDED
    return STATUS_ACTIVE


def campaign_fields(program: dict, today: date | None = None) -> dict:
    """API fields every Program carries: `campaignStatus` always, plus the seasonal
    fields (occasion, discountPercent, validFrom, validUntil) when set."""
    out: dict = {}
    if program.get("occasion"):
        out["occasion"] = program["occasion"]
    if program.get("discount_percent") is not None:
        out["discountPercent"] = program["discount_percent"]
    for key, col in (("validFrom", "valid_from"), ("validUntil", "valid_until")):
        d = as_date(program.get(col))
        if d is not None:
            out[key] = d.isoformat()
    out["campaignStatus"] = campaign_status(program, today)
    return out


def require_active(program: dict, today: date | None = None) -> None:
    """Raise 422 `campaign_not_active` (details: status, validFrom?, validUntil?) unless
    the campaign is `active`."""
    status = campaign_status(program, today)
    if status == STATUS_ACTIVE:
        return
    details: dict = {"status": status}
    for key, col in (("validFrom", "valid_from"), ("validUntil", "valid_until")):
        d = as_date(program.get(col))
        if d is not None:
            details[key] = d.isoformat()
    raise CampaignNotActive(f"campaign is not active ({status})", details=details)


def validate_seasonal(row: dict) -> None:
    """Seasonal shape rules on a (merged) programs row: both dates, validUntil >=
    validFrom, and at least one of discount_percent / non-blank reward."""
    start, end = as_date(row.get("valid_from")), as_date(row.get("valid_until"))
    if start is None or end is None:
        raise ValidationFailed("a seasonal campaign requires validFrom and validUntil")
    if end < start:
        raise ValidationFailed("validUntil must be on or after validFrom")
    if row.get("discount_percent") is None and not (row.get("reward") or "").strip():
        raise ValidationFailed(
            "a seasonal campaign needs an offer: discountPercent and/or reward text")
