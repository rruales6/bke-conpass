"""notifications service — settings, audiences, manual sends and history (Phase 16).

Delivery itself is the `notifier` Lambda's job (conpass_common.notify.dispatch); this
service only records intent: settings, `notifications` rows and — for send-now — the
audience expansion RPC that fills the outbox. Owner-only (the demo owner is an owner).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.demo import DEMO_DAILY_MANUAL_SENDS, is_demo_merchant, manual_sends_today
from conpass_common.errors import Conflict, Forbidden, NotFound, RateLimited, ValidationFailed
from conpass_common.models import (
    Notification,
    NotificationAudiences,
    NotificationCreate,
    NotificationSettings,
    NotificationSettingsUpdate,
)
from conpass_common.notify.settings import (
    MerchantNotificationSettings,
    load_merchant_settings,
    load_platform_rules,
    platform_rules_model,
    save_merchant_settings,
)
from conpass_common.plans import (
    AUTOMATED_CAMPAIGNS,
    MANUAL_CAMPAIGNS,
    require_feature,
    tier_for_merchant,
)
from fastapi import Query

app = create_app(service="notifications")

MAX_SCHEDULE_AHEAD = timedelta(days=60)
LEVEL_RANGE = range(2, 11)          # levelMin counts shown in the composer


def _owner_merchant(identity) -> str:
    identity.require_role("merchant_owner")
    if identity.merchant_id is None:
        raise Forbidden("a merchant account is required")
    return identity.merchant_id


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def settings_to_api(s: MerchantNotificationSettings, rules) -> dict:
    return NotificationSettings.model_validate({
        "automations": {
            "balance": {"enabled": s.balance_on},
            "ready": {"enabled": s.ready_on},
            "near": {"enabled": s.near_on, "stamps": s.near_stamps, "points": s.near_points},
            "milestone": {"enabled": s.milestone_on, "every": s.milestone_every},
            "expiry": {"enabled": s.expiry_on, "warnDays": s.expiry_warn_days},
            "inactive": {"enabled": s.inactive_on, "days": s.inactive_days},
        },
        "balanceExpiryDays": s.balance_expiry_days,
        "cardLocations": {"enrolled": s.card_locations_enrolled,
                          "topVisited": s.card_locations_top},
        "platformRules": platform_rules_model(rules),
    }).model_dump(mode="json")


def settings_from_api(body: NotificationSettingsUpdate) -> MerchantNotificationSettings:
    a = body.automations
    return MerchantNotificationSettings(
        balance_on=a.balance.enabled, ready_on=a.ready.enabled,
        near_on=a.near.enabled, near_stamps=a.near.stamps, near_points=a.near.points,
        milestone_on=a.milestone.enabled, milestone_every=a.milestone.every,
        expiry_on=a.expiry.enabled, expiry_warn_days=a.expiry.warnDays,
        inactive_on=a.inactive.enabled, inactive_days=a.inactive.days,
        balance_expiry_days=body.balanceExpiryDays,
        card_locations_enrolled=body.cardLocations.enrolled,
        card_locations_top=body.cardLocations.topVisited)


def validate_settings(new: MerchantNotificationSettings) -> None:
    """422 rule the DB constraint would otherwise surface as a 500. expiry enabled with no
    window is accepted: it is the inert default state (nothing expires, nothing warned)."""
    days = new.balance_expiry_days
    if days is not None and new.expiry_warn_days >= days:
        raise ValidationFailed(
            "expiry warnDays must be below balanceExpiryDays",
            details={"reason": "warn_not_below_expiry"})


@app.get("/notifications/settings")
def get_settings(identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    return settings_to_api(load_merchant_settings(client, merchant_id),
                           load_platform_rules(client))


@app.put("/notifications/settings")
def put_settings(body: NotificationSettingsUpdate, identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    new = settings_from_api(body)
    validate_settings(new)
    if new.inactive_on and not load_merchant_settings(client, merchant_id).inactive_on:
        require_feature(tier_for_merchant(client, merchant_id), AUTOMATED_CAMPAIGNS)
    save_merchant_settings(client, merchant_id, new)
    return settings_to_api(new, load_platform_rules(client))


# --------------------------------------------------------------------------- #
# Audiences
# --------------------------------------------------------------------------- #
def audience_count(client, merchant_id: str, audience: str, *, level_min: int | None = None,
                   location_id: str | None = None) -> int:
    """Cards an audience reaches now. Counted by the server (`count=exact`) so the
    PostgREST 1000-row page cap cannot truncate it."""
    res = (client.rpc("notification_audience",
                      {"p_merchant": merchant_id, "p_audience": audience,
                       "p_level_min": level_min, "p_location": location_id},
                      count="exact").limit(1).execute())
    return res.count if res.count is not None else len(res.data or [])


@app.get("/notifications/audiences")
def get_audiences(identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    locations = (client.table("merchant_locations").select("id,name")
                 .eq("merchant_id", merchant_id).eq("active", True)
                 .order("created_at").execute().data or [])
    return NotificationAudiences.model_validate({
        "all": audience_count(client, merchant_id, "all"),
        "near": audience_count(client, merchant_id, "near"),
        "inactive": audience_count(client, merchant_id, "inactive"),
        "levels": [{"levelMin": n, "count": audience_count(
            client, merchant_id, "level", level_min=n)} for n in LEVEL_RANGE],
        "locations": [{"locationId": loc["id"], "name": loc["name"], "count": audience_count(
            client, merchant_id, "location", location_id=loc["id"])} for loc in locations],
    }).model_dump(mode="json", exclude_none=True)


# --------------------------------------------------------------------------- #
# Manual sends
# --------------------------------------------------------------------------- #
def requires_manual_campaigns(body: NotificationCreate) -> bool:
    """Everything beyond a plain `general` to `all` without a link is Growth+."""
    return (body.type.value != "general" or body.audience.value != "all"
            or bool(body.url))


def validate_create(body: NotificationCreate, now: datetime) -> None:
    kind, audience = body.type.value, body.audience.value
    if audience == "level" and body.levelMin is None:
        raise ValidationFailed("levelMin is required for the level audience")
    if audience == "location" and body.locationId is None:
        raise ValidationFailed("locationId is required for the location audience")
    if kind == "event" and body.eventAt is None:
        raise ValidationFailed("eventAt is required for an event")
    if kind == "flash" and body.flashHours is None:
        raise ValidationFailed("flashHours is required for a flash offer")
    if body.scheduledAt is not None and not (now < body.scheduledAt <= now + MAX_SCHEDULE_AHEAD):
        raise ValidationFailed("scheduledAt must be in the future, at most 60 days ahead")


def build_row(body: NotificationCreate, merchant_id: str, user_id: str) -> dict:
    """The `notifications` insert; fields foreign to the chosen type/audience are dropped."""
    kind, audience = body.type.value, body.audience.value
    return {
        "merchant_id": merchant_id, "source": "manual", "type": kind, "audience": audience,
        "level_min": body.levelMin if audience == "level" else None,
        "location_id": str(body.locationId) if audience == "location" else None,
        "title": (body.title or "").strip() or None, "body": body.body, "url": body.url,
        "event_at": body.eventAt.isoformat() if kind == "event" and body.eventAt else None,
        "flash_hours": body.flashHours if kind == "flash" else None,
        "status": "scheduled" if body.scheduledAt else "queued",
        "scheduled_at": body.scheduledAt.isoformat() if body.scheduledAt else None,
        "created_by": user_id,
    }


def _location_names(client, rows: list[dict]) -> dict[str, str]:
    ids = list({r["location_id"] for r in rows if r.get("location_id")})
    if not ids:
        return {}
    found = client.table("merchant_locations").select("id,name").in_("id", ids).execute().data
    return {r["id"]: r["name"] for r in found or []}


def notification_to_api(row: dict, names: dict[str, str]) -> dict:
    """`notification_history_view` row -> contract Notification (absent optionals omitted)."""
    out = {
        "id": row["id"], "source": row["source"], "type": row["type"],
        "audience": row["audience"], "level_min": row.get("level_min"),
        "locationId": row.get("location_id"), "locationName": names.get(row.get("location_id")),
        "title": row.get("title"), "body": row["body"], "url": row.get("url"),
        "eventAt": row.get("event_at"), "flashHours": row.get("flash_hours"),
        "status": row["status"], "scheduledAt": row.get("scheduled_at"),
        "sentAt": row.get("sent_at"), "day": row.get("day"),
        "recipients": row.get("recipient_count", row.get("recipients", 0)) or 0,
        "delivered": row.get("delivered", 0) or 0, "visits48h": row.get("visits_48h", 0) or 0,
        "createdAt": row["created_at"],
    }
    out["levelMin"] = out.pop("level_min")
    return Notification.model_validate(
        {k: v for k, v in out.items() if v is not None}).model_dump(mode="json",
                                                                    exclude_none=True)


def _history_row(client, notification_id: str) -> dict:
    rows = (client.table("notification_history_view").select("*")
            .eq("id", notification_id).limit(1).execute().data or [])
    if not rows:
        raise NotFound("notification not found")
    return rows[0]


@app.post("/notifications", status_code=202)
def create_notification(body: NotificationCreate, identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    if requires_manual_campaigns(body):
        require_feature(tier_for_merchant(client, merchant_id), MANUAL_CAMPAIGNS)
    validate_create(body, datetime.now(UTC))
    if body.audience.value == "location":
        owned = (client.table("merchant_locations").select("id")
                 .eq("id", str(body.locationId)).eq("merchant_id", merchant_id)
                 .limit(1).execute().data)
        if not owned:
            raise ValidationFailed("locationId is not a locale of this merchant")
    if (is_demo_merchant(client, merchant_id)
            and manual_sends_today(client, merchant_id) >= DEMO_DAILY_MANUAL_SENDS):
        raise RateLimited("the public demo allows at most "
                          f"{DEMO_DAILY_MANUAL_SENDS} manual sends per day",
                          details={"reason": "demo_daily_limit",
                                   "limit": DEMO_DAILY_MANUAL_SENDS})
    created = client.table("notifications").insert(
        build_row(body, merchant_id, identity.user_id)).execute().data[0]
    if created["status"] == "queued":
        try:
            client.rpc("enqueue_manual_notification", {"p_notification": created["id"]}).execute()
        except Exception:
            # Never leave a half-created send for the dispatcher to close as "sent, 0".
            client.table("notifications").update({"status": "cancelled"}).eq(
                "id", created["id"]).execute()
            raise
    row = _history_row(client, created["id"])
    return notification_to_api(row, _location_names(client, [row]))


@app.get("/notifications")
def list_notifications(identity: CurrentIdentity,
                       limit: Annotated[int, Query(ge=1, le=200)] = 50):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    rows = (client.table("notification_history_view").select("*")
            .eq("merchant_id", merchant_id).order("created_at", desc=True)
            .limit(limit).execute().data or [])
    names = _location_names(client, rows)
    return [notification_to_api(r, names) for r in rows]


@app.delete("/notifications/{notification_id}", status_code=204)
def cancel_notification(notification_id: UUID, identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    notification_id = str(notification_id)
    client = service_client()
    rows = (client.table("notifications").select("id,merchant_id,status")
            .eq("id", notification_id).limit(1).execute().data or [])
    if not rows or rows[0]["merchant_id"] != merchant_id:
        raise NotFound("notification not found")
    if rows[0]["status"] != "scheduled":
        raise Conflict("only a scheduled notification can be cancelled",
                       details={"status": rows[0]["status"]})
    # Guarded on status so a dispatcher that expanded it a moment ago wins the race.
    done = (client.table("notifications").update({"status": "cancelled"})
            .eq("id", notification_id).eq("status", "scheduled").execute().data)
    if not done:
        raise Conflict("the notification already started sending")


handler = lambda_handler(app)
