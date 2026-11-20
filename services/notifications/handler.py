"""notifications service — settings, audiences, manual sends, edits and history (Phases 16, 18).

Delivery itself is the `notifier` Lambda's job (conpass_common.notify.dispatch); this
service only records intent: settings, `notifications` rows and — for send-now — the
audience expansion RPC that fills the outbox. Owner-only (the demo owner is an owner).
"""
from __future__ import annotations

from dataclasses import replace
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
    NotificationUpdate,
)
from conpass_common.notify.automations import automation_stats
from conpass_common.notify.settings import (
    DEFAULT_SEQUENCE,
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
from pydantic import ValidationError as PydanticValidationError

app = create_app(service="notifications")

MAX_SCHEDULE_AHEAD = timedelta(days=60)
EDITABLE_COLUMNS = ("type", "audience", "level_min", "location_id", "title", "body", "url",
                    "event_at", "flash_hours", "scheduled_at")
LEVEL_RANGE = range(2, 11)          # levelMin counts shown in the composer


def _owner_merchant(identity) -> str:
    identity.require_role("merchant_owner")
    if identity.merchant_id is None:
        raise Forbidden("a merchant account is required")
    return identity.merchant_id


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
_TEXT_KEYS = {"ready": "ready", "near": "near", "inactive": "inactive",
              "levelUp": "level_up", "birthday": "birthday"}       # API key -> custom_texts key
INERT_MILESTONE_EVERY = 10
INERT_EXPIRY_WARN_DAYS = 7


def settings_to_api(s: MerchantNotificationSettings, rules, stats: dict | None = None,
                    birthdays_on_file: int = 0) -> dict:
    steps = [{"day": int(x["day"]), "text": str(x.get("text") or "")}
             for x in (s.sequence_steps or [])] or [dict(x) for x in DEFAULT_SEQUENCE]
    birthday = {"enabled": s.birthday_on, "benefit": s.birthday_benefit,
                "validityDays": s.birthday_validity_days}
    if s.birthday_gift:
        birthday["gift"] = s.birthday_gift
    if s.birthday_discount_percent:
        birthday["discountPercent"] = s.birthday_discount_percent
    texts = {api: s.custom_text(db) for api, db in _TEXT_KEYS.items() if s.custom_text(db)}
    out = NotificationSettings.model_validate({
        "automations": {
            "balance": {"enabled": s.balance_on},
            "ready": {"enabled": s.ready_on},
            "near": {"enabled": s.near_on, "stamps": s.near_stamps, "points": s.near_points},
            "inactive": {"enabled": s.inactive_on, "days": s.inactive_days},
            "sequence": {"enabled": s.sequence_on, "steps": steps},
            "levelUp": {"enabled": s.level_up_on},
            "birthday": birthday,
            # Deprecated (Phase 18): retired automations read back disabled.
            "milestone": {"enabled": False, "every": INERT_MILESTONE_EVERY},
            "expiry": {"enabled": False, "warnDays": INERT_EXPIRY_WARN_DAYS},
        },
        "customTexts": texts,
        "cardLocations": {"enrolled": s.card_locations_enrolled,
                          "topVisited": s.card_locations_top},
        "platformRules": platform_rules_model(rules),
        "automationStats": stats or {},
        "birthdaysOnFile": birthdays_on_file,
    }).model_dump(mode="json", exclude_none=True)
    out["balanceExpiryDays"] = None          # O2: balances never expire
    return out


def _texts_from_api(body: NotificationSettingsUpdate) -> dict[str, str]:
    given = body.customTexts.model_dump(exclude_none=True) if body.customTexts else {}
    return {_TEXT_KEYS[k]: v.strip() for k, v in given.items() if v and v.strip()}


def validate_settings(body: NotificationSettingsUpdate) -> None:
    """The 422 rules the contract's field ranges cannot express."""
    seq = body.automations.sequence
    if seq.enabled:
        days = [step.day for step in seq.steps]
        if any(b <= a for a, b in zip(days, days[1:], strict=False)):
            raise ValidationFailed("sequence days must be strictly ascending",
                                   details={"reason": "sequence_days_not_ascending"})
        if any(not step.text.strip() for step in seq.steps):
            raise ValidationFailed("every sequence step needs a text",
                                   details={"reason": "sequence_text_required"})
    bd = body.automations.birthday
    if bd.enabled:
        if bd.benefit.value == "gift" and not (bd.gift or "").strip():
            raise ValidationFailed("birthday gift text is required",
                                   details={"reason": "birthday_gift_required"})
        if bd.benefit.value == "discount" and not bd.discountPercent:
            raise ValidationFailed("birthday discountPercent is required",
                                   details={"reason": "birthday_discount_required"})


def settings_from_api(body: NotificationSettingsUpdate,
                      current: MerchantNotificationSettings) -> MerchantNotificationSettings:
    a = body.automations
    bd = a.birthday
    return replace(
        current,
        balance_on=a.balance.enabled, ready_on=a.ready.enabled,
        near_on=a.near.enabled, near_stamps=a.near.stamps, near_points=a.near.points,
        inactive_on=a.inactive.enabled, inactive_days=a.inactive.days,
        # Retired automations: ignored on write, stored off (columns kept, O2).
        milestone_on=False, expiry_on=False, balance_expiry_days=None,
        sequence_on=a.sequence.enabled,
        sequence_steps=[{"day": st.day, "text": st.text.strip()} for st in a.sequence.steps],
        level_up_on=a.levelUp.enabled,
        birthday_on=bd.enabled, birthday_benefit=bd.benefit.value,
        birthday_gift=(bd.gift or "").strip() or None,
        birthday_discount_percent=bd.discountPercent,
        birthday_validity_days=bd.validityDays,
        custom_texts=_texts_from_api(body),
        card_locations_enrolled=body.cardLocations.enrolled,
        card_locations_top=body.cardLocations.topVisited)


def enforce_pro_gates(client, merchant_id: str, new: MerchantNotificationSettings,
                      current: MerchantNotificationSettings) -> None:
    """402 `feature_not_in_plan` for Pro-only parts the merchant's plan lacks. Turning a
    Pro automation ON, or writing a custom text that is not already stored, needs Pro; what a
    downgraded merchant already has stays saveable so the rest of the form is not blocked."""
    turned_on = ((new.sequence_on and not current.sequence_on)
                 or (new.level_up_on and not current.level_up_on)
                 or (new.birthday_on and not current.birthday_on))
    new_text = any(current.custom_texts.get(k) != v for k, v in new.custom_texts.items())
    if turned_on or new_text:
        require_feature(tier_for_merchant(client, merchant_id), AUTOMATED_CAMPAIGNS)


def birthdays_on_file(client, merchant_id: str) -> int:
    res = (client.table("customers").select("id", count="exact")
           .eq("merchant_id", merchant_id).gte("birthday_month", 1).limit(1).execute())
    return res.count if res.count is not None else len(res.data or [])


def _settings_response(client, merchant_id: str, s: MerchantNotificationSettings) -> dict:
    return settings_to_api(s, load_platform_rules(client),
                           automation_stats(client, merchant_id),
                           birthdays_on_file(client, merchant_id))


@app.get("/notifications/settings")
def get_settings(identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    return _settings_response(client, merchant_id, load_merchant_settings(client, merchant_id))


@app.put("/notifications/settings")
def put_settings(body: NotificationSettingsUpdate, identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    current = load_merchant_settings(client, merchant_id)
    new = settings_from_api(body, current)
    # Plan first: a merchant without Pro hears "not in your plan" (402), not a field error
    # about a Pro-only form they cannot use anyway.
    enforce_pro_gates(client, merchant_id, new, current)
    validate_settings(body)
    save_merchant_settings(client, merchant_id, new)
    return _settings_response(client, merchant_id, new)


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


def check_manual(client, merchant_id: str, body: NotificationCreate) -> None:
    """Plan gate + field rules + locale ownership: what create and edit both enforce."""
    if requires_manual_campaigns(body):
        require_feature(tier_for_merchant(client, merchant_id), MANUAL_CAMPAIGNS)
    validate_create(body, datetime.now(UTC))
    if body.audience.value == "location":
        owned = (client.table("merchant_locations").select("id")
                 .eq("id", str(body.locationId)).eq("merchant_id", merchant_id)
                 .limit(1).execute().data)
        if not owned:
            raise ValidationFailed("locationId is not a locale of this merchant")


@app.post("/notifications", status_code=202)
def create_notification(body: NotificationCreate, identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    client = service_client()
    check_manual(client, merchant_id, body)
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


def merged_create(stored: dict, patch: NotificationUpdate) -> NotificationCreate:
    """The stored scheduled notification with the partial update applied, as a create body."""
    merged = {
        "type": stored["type"], "audience": stored["audience"], "levelMin": stored.get("level_min"),
        "locationId": stored.get("location_id"), "title": stored.get("title"),
        "body": stored["body"], "url": stored.get("url"), "eventAt": stored.get("event_at"),
        "flashHours": stored.get("flash_hours"), "scheduledAt": stored.get("scheduled_at")}
    merged.update(patch.model_dump(mode="json", exclude_unset=True))
    try:
        return NotificationCreate.model_validate(
            {k: v for k, v in merged.items() if v is not None})
    except PydanticValidationError as exc:
        raise ValidationFailed("the edited notification is not valid",
                               details={"errors": exc.errors(include_url=False,
                                                             include_context=False,
                                                             include_input=False)}) from exc


@app.patch("/notifications/{notification_id}")
def update_notification(notification_id: UUID, body: NotificationUpdate,
                        identity: CurrentIdentity):
    merchant_id = _owner_merchant(identity)
    notification_id = str(notification_id)
    client = service_client()
    rows = (client.table("notifications").select("*").eq("id", notification_id)
            .eq("source", "manual").limit(1).execute().data or [])
    if not rows or rows[0]["merchant_id"] != merchant_id:
        raise NotFound("notification not found")
    stored = rows[0]
    if stored["status"] != "scheduled":
        raise Conflict("only a scheduled notification can be edited",
                       details={"status": stored["status"]})
    merged = merged_create(stored, body)
    check_manual(client, merchant_id, merged)     # same rules and gates as create; no demo cap
    fields = {k: v for k, v in build_row(merged, merchant_id, identity.user_id).items()
              if k in EDITABLE_COLUMNS}
    # Guarded on status so a dispatcher that expanded it a moment ago wins the race.
    done = (client.table("notifications").update(fields).eq("id", notification_id)
            .eq("status", "scheduled").execute().data)
    if not done:
        raise Conflict("the notification already started sending")
    row = _history_row(client, notification_id)
    return notification_to_api(row, _location_names(client, [row]))


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
