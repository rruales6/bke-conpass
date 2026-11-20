"""Notification settings — the ONE place that reads them (Phase 16).

Merchant settings live in `merchant_notification_settings` (missing row = DEFAULTS, which
mirror the 0017 column defaults: balance off, "Te extrañamos" on at 30 days, Pro parts off);
platform rules in the `platform_notification_rules` singleton. Both the notifications
service and wallet_sync (expiry date, per-card locations) read through here so defaults
cannot drift.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

QUIET_START = "21:00"
QUIET_END = "08:00"
QUIET_START_HOUR = 21
QUIET_END_HOUR = 8
TIMEZONE = "America/Guayaquil"   # fixed UTC-5, no DST (conpass_common.campaigns.ECUADOR_TZ)
INACTIVE_AUDIENCE_DAYS = 30      # manual "Sin visitar en 30 días"
MAX_ACTIVE_LOCATIONS = 10        # Google: 10 merchantLocations per class and per object


SEQUENCE_MAX_STEPS = 3
INACTIVE_DAYS_RANGE = (7, 89)         # before the 90-day pause (O2)
CUSTOM_TEXT_KEYS = ("ready", "near", "inactive", "level_up", "birthday")   # custom_texts jsonb
DEFAULT_SEQUENCE = ({"day": 30, "text": ""}, {"day": 45, "text": ""}, {"day": 60, "text": ""})


def _default_sequence() -> list[dict]:
    return [dict(step) for step in DEFAULT_SEQUENCE]


@dataclass(frozen=True)
class MerchantNotificationSettings:
    balance_on: bool = False          # v3.0 decision 2: off by default
    ready_on: bool = True
    near_on: bool = True
    near_stamps: int = 1
    near_points: int = 10
    # DEPRECATED (O2 / Phase 18): inert, never written as "on". Kept because the columns and
    # wallet_sync still read them (balance_expiry_days is always None now).
    milestone_on: bool = False
    milestone_every: int = 10
    expiry_on: bool = False
    expiry_warn_days: int = 7
    inactive_on: bool = True          # "Te extrañamos", every plan since v3.0
    inactive_days: int = 30
    balance_expiry_days: int | None = None
    card_locations_enrolled: bool = True
    card_locations_top: bool = False
    # Pro (automated_campaigns) -------------------------------------------------------
    custom_texts: dict = field(default_factory=dict)       # keys: CUSTOM_TEXT_KEYS
    sequence_on: bool = False
    sequence_steps: list = field(default_factory=_default_sequence)   # [{day, text}] <= 3
    level_up_on: bool = False
    birthday_on: bool = False
    birthday_benefit: str = "gift"    # gift | discount
    birthday_gift: str | None = None
    birthday_discount_percent: int | None = None
    birthday_validity_days: int = 7

    def as_row(self) -> dict:
        return asdict(self)

    def custom_text(self, key: str) -> str | None:
        """The merchant's own wording for `key`, or None (blank = the fixed template)."""
        value = (self.custom_texts or {}).get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None


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
    return settings_from_row(rows[0])


def settings_from_row(row: dict) -> MerchantNotificationSettings:
    """A stored row (possibly with NULLs / missing new columns) as settings."""
    values = {k: row[k] for k in _MERCHANT_FIELDS if row.get(k) is not None}
    if not isinstance(values.get("custom_texts", {}), dict):
        values.pop("custom_texts")
    if not isinstance(values.get("sequence_steps", []), list):
        values.pop("sequence_steps")
    return MerchantNotificationSettings(**values)


def is_pro(client, merchant_id: str) -> bool:
    """True while the merchant's plan includes automated_campaigns. Pro automations check
    this at RUN time, so a downgraded merchant stops sending them even with the flags on."""
    from ..plans import AUTOMATED_CAMPAIGNS, get_plan, tier_for_merchant
    try:
        return get_plan(tier_for_merchant(client, merchant_id)).has(AUTOMATED_CAMPAIGNS)
    except Exception:  # noqa: BLE001 - no subscription row: no Pro-only automation
        return False


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
