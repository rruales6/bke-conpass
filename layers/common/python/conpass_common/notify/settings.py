"""Notification settings — the ONE place that reads them (Phase 16).

Merchant settings live in `merchant_notification_settings` (missing row = DEFAULTS);
platform rules in the `platform_notification_rules` singleton. Both the notifications
service and wallet_sync (expiry date, per-card locations) read through here so defaults
cannot drift.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

QUIET_START = "21:00"
QUIET_END = "08:00"
QUIET_START_HOUR = 21
QUIET_END_HOUR = 8
TIMEZONE = "America/Guayaquil"   # fixed UTC-5, no DST (conpass_common.campaigns.ECUADOR_TZ)
INACTIVE_AUDIENCE_DAYS = 30      # manual "Sin visitar en 30 días"
MAX_ACTIVE_LOCATIONS = 10        # Google: 10 merchantLocations per class and per object


@dataclass(frozen=True)
class MerchantNotificationSettings:
    balance_on: bool = True
    ready_on: bool = True
    near_on: bool = True
    near_stamps: int = 1
    near_points: int = 10
    milestone_on: bool = True
    milestone_every: int = 10
    expiry_on: bool = True
    expiry_warn_days: int = 7
    inactive_on: bool = False
    inactive_days: int = 28
    balance_expiry_days: int | None = None
    card_locations_enrolled: bool = True
    card_locations_top: bool = False

    def as_row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PlatformRules:
    cap_per_day: int = 2
    quiet_hours_enabled: bool = True


_MERCHANT_FIELDS = tuple(MerchantNotificationSettings.__dataclass_fields__)


def load_merchant_settings(client, merchant_id: str) -> MerchantNotificationSettings:
    rows = (client.table("merchant_notification_settings").select("*")
            .eq("merchant_id", merchant_id).limit(1).execute().data or [])
    if not rows:
        return MerchantNotificationSettings()
    row = rows[0]
    return MerchantNotificationSettings(**{k: row[k] for k in _MERCHANT_FIELDS if k in row})


def save_merchant_settings(client, merchant_id: str,
                           value: MerchantNotificationSettings) -> None:
    client.table("merchant_notification_settings").upsert(
        {"merchant_id": merchant_id, **value.as_row(), "updated_at": datetime.now(UTC).isoformat()},
        on_conflict="merchant_id").execute()


def load_platform_rules(client) -> PlatformRules:
    rows = (client.table("platform_notification_rules").select("*")
            .limit(1).execute().data or [])
    if not rows:
        return PlatformRules()
    r = rows[0]
    return PlatformRules(cap_per_day=r["cap_per_day"],
                         quiet_hours_enabled=r["quiet_hours_enabled"])


def platform_rules_model(rules: PlatformRules) -> dict:
    """API shape (PlatformNotificationRules)."""
    return {"capPerDay": rules.cap_per_day, "quietHoursEnabled": rules.quiet_hours_enabled,
            "quietStart": QUIET_START, "quietEnd": QUIET_END, "timezone": TIMEZONE}
