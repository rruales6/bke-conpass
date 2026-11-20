"""Billing workflow (Phase 18, D21 O4): payments ledger, plan-change requests, activity log.

Shared by the merchants service (owner side: signup, plan change, payments list) and the admin
service (activation, payments, plan-change review), plus the notifier's daily job
(`apply_scheduled_plan_changes`). Everything here takes a Data API `client`, so it works the
same in a handler, the notifier and a test with a fake client.

Money is IVA included unless a name says otherwise (plans.py rules); nothing here hard-codes a
price: it all comes from the catalog helpers.
"""
from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from decimal import Decimal

from .errors import NotFound
from .logging import get_logger
from .plans import (
    ANNUAL,
    LAUNCH,
    MONTHLY,
    PLANS,
    TIER_ORDER,
    Estimate,
    current_pricing_phase,
    estimate,
    get_plan,
    money,
    period_start,
    plan_to_api,
    subscription_price_usd,
    subscription_to_api,
)
from .usage import notifications_used

log = get_logger(__name__)

# plan_change_requests.status values that block a new request (one open request per merchant).
OPEN_STATUSES = ("awaiting_payment", "pending_review", "scheduled")
# Requests an admin can still approve or reject.
REVIEWABLE_STATUSES = ("pending_review", "awaiting_payment")

ACTIVITY_ACTIONS = (
    "signed_up", "proof_uploaded", "activated", "rejected", "payment_recorded", "reminder_sent",
    "plan_change_requested", "plan_changed", "plan_change_rejected", "suspended",
    "reactivated", "credentials_resent", "location_added",
)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def today_utc() -> date:
    """Today (UTC). One seam so tests can pin the date."""
    return datetime.now(UTC).date()


def as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def add_period(start: date, period: str) -> date:
    """`start` plus one billing period: a month or a year, clamping the day (Jan 31 -> Feb 28)."""
    if period == ANNUAL:
        year, month = start.year + 1, start.month
    else:
        year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def first_of_next_month(today: date) -> date:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Activity log (merchant_activity)
# --------------------------------------------------------------------------- #
def log_activity(client, merchant_id: str, action: str, *, actor_user_id: str | None = None,
                 actor_role: str, details: dict | None = None) -> None:
    """Append one row to the merchant's activity log (board 18). Best-effort: it is written
    AFTER the state change it describes, and a failed audit line must not turn a committed
    change into an error response."""
    row = {"merchant_id": merchant_id, "action": action, "actor_role": actor_role,
           "details": details or {}}
    if actor_user_id:
        row["actor_user_id"] = actor_user_id
    try:
        client.table("merchant_activity").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("merchant_activity_failed", extra={
            "merchant_id": merchant_id, "action": action, "error": type(exc).__name__})


def activity_to_api(row: dict) -> dict:
    out = {"id": row["id"], "action": row["action"], "createdAt": row["created_at"]}
    if row.get("actor_role"):
        out["actorRole"] = row["actor_role"]
    if row.get("details"):
        out["details"] = row["details"]
    return out


def list_activity(client, merchant_id: str, limit: int = 50) -> list[dict]:
    rows = (client.table("merchant_activity").select("*").eq("merchant_id", merchant_id)
            .order("created_at", desc=True).limit(limit).execute().data or [])
    return [activity_to_api(r) for r in rows]


# --------------------------------------------------------------------------- #
# Payments ledger
# --------------------------------------------------------------------------- #
def payment_to_api(row: dict) -> dict:
    out = {
        "id": row["id"], "kind": row["kind"], "amountUsd": float(row["amount_usd"]),
        "method": row["method"], "status": row["status"],
        "hasReceipt": bool(row.get("proof_storage_key")), "createdAt": row["created_at"],
    }
    for api, col in (("tier", "tier"), ("billingPeriod", "billing_period"),
                     ("periodStart", "period_start"), ("periodEnd", "period_end"),
                     ("paidAt", "paid_at"), ("note", "note"),
                     ("rejectionReason", "rejection_reason")):
        if row.get(col) is not None:
            out[api] = row[col]
    return out


def create_payment(client, row: dict) -> dict:
    return client.table("payments").insert(row).execute().data[0]


def get_payment(client, payment_id: str) -> dict | None:
    rows = client.table("payments").select("*").eq("id", payment_id).execute().data
    return rows[0] if rows else None


def update_payment(client, payment_id: str, patch: dict) -> dict:
    rows = client.table("payments").update(patch).eq("id", payment_id).execute().data
    return rows[0] if rows else {}


def list_payments(client, merchant_id: str, limit: int = 200) -> list[dict]:
    """Newest first."""
    return (client.table("payments").select("*").eq("merchant_id", merchant_id)
            .order("created_at", desc=True).limit(limit).execute().data or [])


def signup_payment(client, merchant_id: str) -> dict | None:
    """The merchant's latest signup payment (the receipt the admin verifies)."""
    rows = (client.table("payments").select("*").eq("merchant_id", merchant_id)
            .eq("kind", "signup").order("created_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def payment_receipt_link(client, merchant_id: str, payment_id: str) -> dict:
    """Presigned GET for a payment's receipt. 404 when it has none or is another merchant's.
    Shared by GET /merchants/{id}/payments/{pid}/receipt and the admin twin."""
    from .assets import presign_payment_proof_download
    from .errors import NotConfigured

    payment = get_payment(client, payment_id)
    key = payment.get("proof_storage_key") if payment else None
    if payment is None or payment["merchant_id"] != merchant_id or not key:
        raise NotFound("this payment has no receipt")
    try:
        url = presign_payment_proof_download(key)
    except RuntimeError as exc:
        raise NotConfigured(str(exc)) from exc
    return {"url": url, "uploadedAt": payment["created_at"]}


def confirm_payment(client, payment_id: str, *, confirmed_by: str | None, paid_at: date,
                    period_start_: date | None = None, period_end: date | None = None) -> dict:
    patch = {"status": "confirmed", "paid_at": paid_at.isoformat(),
             "confirmed_at": now_iso(), "rejection_reason": None}
    if confirmed_by:
        patch["confirmed_by"] = confirmed_by
    if period_start_:
        patch["period_start"] = period_start_.isoformat()
    if period_end:
        patch["period_end"] = period_end.isoformat()
    return update_payment(client, payment_id, patch)


def reject_payment(client, payment_id: str, reason: str) -> dict:
    return update_payment(client, payment_id, {"status": "rejected", "rejection_reason": reason})


# --------------------------------------------------------------------------- #
# Plan-change requests
# --------------------------------------------------------------------------- #
def plan_change_direction(from_tier: str, from_period: str, to_tier: str,
                          to_period: str) -> str | None:
    """'upgrade' | 'downgrade' | None (nothing changes).

    A tier change dominates the period: a higher tier is an upgrade even if it moves from
    annual to monthly. With the same tier, monthly -> annual is an upgrade (pay now, 20 % off)
    and annual -> monthly is a downgrade (applied at renewal)."""
    if from_tier != to_tier:
        return "upgrade" if TIER_ORDER.index(to_tier) > TIER_ORDER.index(from_tier) \
            else "downgrade"
    if from_period == to_period:
        return None
    return "upgrade" if to_period == ANNUAL else "downgrade"


def plan_change_to_api(row: dict, has_receipt: bool = False) -> dict:
    out = {
        "id": row["id"], "fromTier": row["from_tier"], "toTier": row["to_tier"],
        "fromPeriod": row["from_period"], "toPeriod": row["to_period"],
        "direction": row["direction"], "status": row["status"],
        "hasReceipt": has_receipt, "createdAt": row["created_at"],
    }
    for api, col in (("amountUsd", "amount_usd"), ("effectiveAt", "effective_at"),
                     ("rejectionReason", "rejection_reason"), ("resolvedAt", "resolved_at")):
        if row.get(col) is not None:
            out[api] = float(row[col]) if col == "amount_usd" else row[col]
    return out


def _receipt_flags(client, requests: list[dict]) -> dict[str, bool]:
    """payment_id -> has a receipt, for these requests, in one query."""
    ids = [r["payment_id"] for r in requests if r.get("payment_id")]
    if not ids:
        return {}
    rows = client.table("payments").select("id,proof_storage_key").in_("id", ids).execute().data
    return {r["id"]: bool(r.get("proof_storage_key")) for r in rows or []}


def plan_changes_to_api(client, requests: list[dict]) -> list[dict]:
    flags = _receipt_flags(client, requests)
    return [plan_change_to_api(r, flags.get(r.get("payment_id"), False)) for r in requests]


def get_plan_change(client, request_id: str) -> dict | None:
    rows = client.table("plan_change_requests").select("*").eq("id", request_id).execute().data
    return rows[0] if rows else None


def open_plan_change(client, merchant_id: str) -> dict | None:
    """The merchant's open request (at most one: partial unique index)."""
    rows = (client.table("plan_change_requests").select("*").eq("merchant_id", merchant_id)
            .in_("status", list(OPEN_STATUSES)).order("created_at", desc=True)
            .limit(1).execute().data)
    return rows[0] if rows else None


def open_plan_change_api(client, merchant_id: str) -> dict | None:
    row = open_plan_change(client, merchant_id)
    return plan_changes_to_api(client, [row])[0] if row else None


def list_plan_changes(client, merchant_id: str, limit: int = 10) -> list[dict]:
    return (client.table("plan_change_requests").select("*").eq("merchant_id", merchant_id)
            .order("created_at", desc=True).limit(limit).execute().data or [])


def update_plan_change(client, request_id: str, patch: dict) -> dict:
    rows = client.table("plan_change_requests").update(patch).eq("id", request_id).execute().data
    return rows[0] if rows else {}


def term_patch(tier: str, period: str, phase: str) -> dict:
    """Subscription columns for starting `tier` / `period` now. Monthly follows the catalog
    (no lock); an annual term locks the annual price at today's phase until renewal."""
    if period == ANNUAL:
        return {"tier": tier, "billing_period": ANNUAL,
                "locked_price_usd": float(get_plan(tier).annual_usd(phase))}
    return {"tier": tier, "billing_period": MONTHLY, "locked_price_usd": None}


# --------------------------------------------------------------------------- #
# Subscription + usage
# --------------------------------------------------------------------------- #
def get_subscription(client, merchant_id: str) -> dict | None:
    rows = client.table("subscriptions").select("*").eq("merchant_id", merchant_id).execute().data
    return rows[0] if rows else None


def subscription_for_api(client, sub: dict, *, active_cards: int | None = None,
                         phase: str | None = None, with_pending: bool = True) -> dict:
    """`Subscription` for the API, with the open plan change attached when there is one."""
    phase = phase or current_pricing_phase(client)
    out = subscription_to_api(sub, active_cards, phase)
    if with_pending:
        pending = open_plan_change_api(client, sub["merchant_id"])
        if pending:
            out["pendingChange"] = pending
    return out


def merchant_to_api(m: dict, subscription: dict | None = None) -> dict:
    out = {
        "id": m["id"], "businessName": m["business_name"], "ruc": m.get("ruc"),
        "category": m.get("category"), "city": m.get("city"),
        "contactName": m.get("contact_name"), "contactEmail": m.get("contact_email"),
        "contactPhone": m.get("contact_phone"),
        "logoStorageKey": m.get("logo_storage_key"), "createdAt": m["created_at"],
    }
    if subscription:
        out["subscription"] = subscription
    return out


def month_estimate(sub: dict, active_cards: int, notifications: int, phase: str,
                   today: date | None = None) -> Estimate:
    """This month's charge for a subscription (the figure GET usage and the admin list share).

    - Monthly: the subscription price every month.
    - Annual: the (locked) price only in the renewal month (next_charge_at in the current month,
      already past, or not set yet because the signup is unpaid), else just the overage.
    """
    today = today or today_utc()
    plan = get_plan(sub["tier"])
    price = subscription_price_usd(sub, phase)
    if (sub.get("billing_period") or MONTHLY) == ANNUAL:
        nxt = as_date(sub.get("next_charge_at"))
        if nxt is not None and (nxt.year, nxt.month) > (today.year, today.month):
            price = Decimal("0")
    return estimate(plan, active_cards, notifications, subscription_usd=price, phase=phase)


def _count(client, table: str, *, key: str = "id", **eq) -> int:
    q = client.table(table).select(key, count="exact")
    for col, val in eq.items():
        q = q.eq(col, val)
    res = q.execute()
    return res.count if res.count is not None else len(res.data or [])


def active_card_count(client, merchant_id: str) -> int:
    rows = (client.table("merchant_active_passes").select("*")
            .eq("merchant_id", merchant_id).execute().data)
    return rows[0]["active_pass_count"] if rows else 0


def usage_for(client, merchant_id: str) -> dict:
    """`PlanUsage` for one merchant: GET /merchants/{id}/usage and the admin file share it."""
    sub = get_subscription(client, merchant_id)
    if sub is None:
        raise NotFound("subscription not found")
    phase = current_pricing_phase(client)
    today = today_utc()
    start = period_start(today)
    plan = get_plan(sub["tier"])
    active = active_card_count(client, merchant_id)
    notifs = notifications_used(client, merchant_id, start)
    est = month_estimate(sub, active, notifs, phase, today)
    return {
        "plan": plan_to_api(plan, phase),
        "activeCards": active,
        "extraCards": est.extra_cards,
        "extraCardsUsd": float(est.extra_cards_usd),
        "notificationsUsed": notifs,
        "extraNotifications": est.extra_notifications,
        "extraNotificationsUsd": float(est.extra_notifications_usd),
        "estimatedTotalUsd": float(est.total_usd),
        "subscriptionUsd": float(est.subscription_usd),
        "ivaUsd": float(est.iva_usd),
        "billingPeriod": sub.get("billing_period") or MONTHLY,
        "locationCount": _count(client, "merchant_locations", merchant_id=merchant_id,
                                active=True),
        "programCount": _count(client, "programs", merchant_id=merchant_id),
        "operationUserCount": _count(client, "profiles", key="user_id", merchant_id=merchant_id,
                                     role="operation_user"),
        "periodStart": start.isoformat(),
    }


def signup_amount(tier: str, period: str, phase: str = LAUNCH) -> Decimal:
    return money(PLANS[tier].price_for(period, phase))


# --------------------------------------------------------------------------- #
# Daily job (notifier)
# --------------------------------------------------------------------------- #
def apply_scheduled_plan_changes(client, today: date) -> int:
    """Apply every `plan_change_requests` row with status 'scheduled' and effective_at <= today
    (downgrades): set the subscription's tier/billing_period, clear the price lock when the
    period becomes monthly (an annual term locks the annual price at today's phase), mark the
    request 'applied', log `plan_changed` in merchant_activity. Returns how many were applied.
    Called by the notifier's daily job. Idempotent: re-running the same day applies nothing
    new, because an applied request is no longer 'scheduled'."""
    due = (client.table("plan_change_requests").select("*").eq("status", "scheduled")
           .lte("effective_at", today.isoformat()).execute().data or [])
    if not due:
        return 0
    phase = current_pricing_phase(client)
    applied = 0
    for req in due:
        merchant_id = req["merchant_id"]
        if get_subscription(client, merchant_id) is None:
            log.warning("scheduled_plan_change_no_subscription", extra={"merchant_id": merchant_id})
            continue
        (client.table("subscriptions").update(term_patch(req["to_tier"], req["to_period"], phase))
         .eq("merchant_id", merchant_id).execute())
        update_plan_change(client, req["id"], {"status": "applied", "resolved_at": now_iso()})
        log_activity(client, merchant_id, "plan_changed", actor_role="system", details={
            "from": {"tier": req["from_tier"], "billingPeriod": req["from_period"]},
            "to": {"tier": req["to_tier"], "billingPeriod": req["to_period"]},
            "scheduled": True})
        applied += 1
    return applied
