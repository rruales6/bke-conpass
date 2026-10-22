"""Plan catalog — the ONE source of truth for pricing (business model v2.0 §3, D17).

Two-layer pricing: a monthly subscription that unlocks features, plus consumption
(active cards and notifications above the plan's inclusion). Every price, inclusion
and limit the API returns or enforces comes from `PLANS` (programs are unlimited on
every plan — owner decision, Phase 13); the DB stores only the tier,
and the PWA reads the catalog through `GET /plans`. Enterprise / white-label is quoted
off-catalog and is deliberately not a tier.

Catalog as of modelo-negocio.md v2.0 (October 2026):

    tier     USD/mo  cards incl. / extra   notifications incl. / extra
    starter    4.99     200 / 0.044            500 / 0.015
    growth    14.99   1,000 / 0.034          1,000 / 0.010
    pro       24.99   5,000 / 0.024         10,000 / 0.005

Every plan has seasonal + accumulation campaigns (`campaigns`), reports, user
management and basic notifications; Growth adds "Estás cerca" and manual notification
campaigns; Pro adds automated campaigns (birthdays and special dates that fire alone).

Money is `Decimal` end to end and rounded to cents only at the final figure — the
per-card rates have three decimals (0.044), so rounding per line would drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from conpass_common.errors import FeatureNotInPlan, NotFound

CENT = Decimal("0.01")

# Feature keys (mirror the contract's PlanFeature enum).
CAMPAIGNS = "campaigns"                          # seasonal + accumulation, every plan
BASIC_NOTIFICATIONS = "basic_notifications"      # every plan, within its allowance
REPORTS = "reports"
USER_MANAGEMENT = "user_management"
NEARBY_NOTIFICATIONS = "nearby_notifications"
MANUAL_CAMPAIGNS = "manual_campaigns"
AUTOMATED_CAMPAIGNS = "automated_campaigns"


@dataclass(frozen=True)
class Plan:
    tier: str
    price_usd: Decimal
    included_cards: int
    extra_card_usd: Decimal
    included_notifications: int
    extra_notification_usd: Decimal | None   # None = no per-notification rate (no plan today)
    operation_user_limit: int | None         # None = unlimited
    features: tuple[str, ...]

    def has(self, feature: str) -> bool:
        return feature in self.features


_STARTER_FEATURES = (CAMPAIGNS, BASIC_NOTIFICATIONS, REPORTS, USER_MANAGEMENT)
_GROWTH_FEATURES = _STARTER_FEATURES + (NEARBY_NOTIFICATIONS, MANUAL_CAMPAIGNS)
_PRO_FEATURES = _GROWTH_FEATURES + (AUTOMATED_CAMPAIGNS,)

PLANS: dict[str, Plan] = {
    "starter": Plan(
        tier="starter", price_usd=Decimal("4.99"),
        included_cards=200, extra_card_usd=Decimal("0.044"),
        included_notifications=500, extra_notification_usd=Decimal("0.015"),
        operation_user_limit=2,
        features=_STARTER_FEATURES,
    ),
    "growth": Plan(
        tier="growth", price_usd=Decimal("14.99"),
        included_cards=1000, extra_card_usd=Decimal("0.034"),
        included_notifications=1000, extra_notification_usd=Decimal("0.010"),
        operation_user_limit=5,
        features=_GROWTH_FEATURES,
    ),
    "pro": Plan(
        tier="pro", price_usd=Decimal("24.99"),
        included_cards=5000, extra_card_usd=Decimal("0.024"),
        included_notifications=10000, extra_notification_usd=Decimal("0.005"),
        operation_user_limit=None,
        features=_PRO_FEATURES,
    ),
}

TIER_ORDER: tuple[str, ...] = ("starter", "growth", "pro")


def get_plan(tier: str) -> Plan:
    try:
        return PLANS[tier]
    except KeyError:
        raise ValueError(f"unknown tier {tier!r}") from None


def cheapest_with(feature: str) -> str:
    """The lowest tier that includes `feature` — what an upgrade prompt should name."""
    for tier in TIER_ORDER:
        if PLANS[tier].has(feature):
            return tier
    raise ValueError(f"no plan includes {feature!r}")


def require_feature(tier: str, feature: str) -> None:
    """Raise 402 `feature_not_in_plan` unless the merchant's plan includes `feature`."""
    if not get_plan(tier).has(feature):
        required = cheapest_with(feature)
        raise FeatureNotInPlan(
            f"'{feature}' is not included in the {tier} plan (available from {required})",
            details={"feature": feature, "tier": tier, "requiredTier": required},
        )


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Estimate:
    extra_cards: int
    extra_cards_usd: Decimal
    extra_notifications: int
    extra_notifications_usd: Decimal
    total_usd: Decimal


def estimate(plan: Plan, active_cards: int, notifications_used: int = 0) -> Estimate:
    """Subscription + overage for one month. Exact until the final round to cents."""
    extra_cards = max(0, active_cards - plan.included_cards)
    cards_usd = plan.extra_card_usd * extra_cards
    extra_notifs = max(0, notifications_used - plan.included_notifications)
    notifs_usd = (plan.extra_notification_usd or Decimal(0)) * extra_notifs
    return Estimate(
        extra_cards=extra_cards,
        extra_cards_usd=money(cards_usd),
        extra_notifications=extra_notifs,
        extra_notifications_usd=money(notifs_usd),
        total_usd=money(plan.price_usd + cards_usd + notifs_usd),
    )


def period_start(today: date) -> date:
    """Billing month = calendar month (UTC)."""
    return today.replace(day=1)


def plan_to_api(plan: Plan) -> dict:
    """Plan → contract `Plan` (camelCase; absent optional = unlimited / not offered)."""
    out = {
        "tier": plan.tier,
        "priceUsd": float(plan.price_usd),
        "includedCards": plan.included_cards,
        "extraCardUsd": float(plan.extra_card_usd),
        "includedNotifications": plan.included_notifications,
        "features": list(plan.features),
    }
    if plan.extra_notification_usd is not None:
        out["extraNotificationUsd"] = float(plan.extra_notification_usd)
    if plan.operation_user_limit is not None:
        out["operationUserLimit"] = plan.operation_user_limit
    return out


def next_tier_with_more_operation_users(tier: str) -> str | None:
    """The next tier up that allows more operation users than `tier` (None = none does)."""
    current = get_plan(tier).operation_user_limit
    if current is None:
        return None
    for candidate in TIER_ORDER[TIER_ORDER.index(tier) + 1:]:
        limit = PLANS[candidate].operation_user_limit
        if limit is None or limit > current:
            return candidate
    return None


def subscription_to_api(sub: dict, active_cards: int | None = None) -> dict:
    """`subscriptions` row -> contract `Subscription`, derived from the catalog.

    The row stores only tier + payment state; price, inclusion and limits come from
    `PLANS`. Shared by merchants (onboarding, GET /merchants/{id}) and admin (PATCH
    subscription) so the two Lambdas cannot drift. `activeCards` is included only when
    the caller has the count; an unlimited operation-user limit is omitted.
    """
    plan = get_plan(sub["tier"])
    out = {
        "tier": plan.tier,
        "paymentStatus": sub["payment_status"],
        "priceUsd": float(plan.price_usd),
        "includedCards": plan.included_cards,
        "nextChargeAt": sub.get("next_charge_at"),
        "lastPaymentAt": sub.get("last_payment_at"),
    }
    if plan.operation_user_limit is not None:
        out["operationUserLimit"] = plan.operation_user_limit
    if active_cards is not None:
        out["activeCards"] = active_cards
    return out


def tier_for_merchant(client, merchant_id: str) -> str:
    """The merchant's tier from `subscriptions` (for feature gating in any service)."""
    rows = (client.table("subscriptions").select("tier")
            .eq("merchant_id", merchant_id).execute().data)
    if not rows:
        raise NotFound("no subscription found for this merchant")
    return rows[0]["tier"]
