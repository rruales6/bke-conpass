"""Plan catalog: the ONE source of truth for pricing (business model v3.0, D17 as revised by D21).

There are two layers of charges:
- a subscription that unlocks features;
- consumption: active cards and notifications above the plan's inclusion.

Every price, inclusion and limit the API returns or enforces comes from `PLANS`. Programs are
unlimited on every plan. The DB stores only the tier, the billing period and, for an annual
term, the price that was paid (`subscriptions.locked_price_usd`). The PWA reads the catalog
through `GET /plans`. Enterprise is quoted by contact form and is deliberately not a tier.

Catalog as of modelo-negocio.md v3.0 (November 2026). Plan prices INCLUDE IVA and end in .99;
overage is charged plus IVA.

    tier     monthly launch/list  annual launch/list  cards incl./extra  notifs incl./extra  br.
    starter     5.99 /  7.99       56.99 /  75.99       200 / 0.044        500 / 0.015      1
    growth     17.99 / 22.99      171.99 / 219.99     1,000 / 0.034      3,000 / 0.010      3
    pro        57.99 / 74.99      555.99 / 718.99     5,000 / 0.024     10,000 / 0.005     10

Cashiers are unlimited on every plan.

Pricing phase (platform_settings.pricing_phase):
- 'launch' (today) charges the launch price and shows the list price struck through.
- 'list' starts when Apple Wallet ships, with 30 days' notice.
- An annual term keeps the price it was paid at until renewal.

Features (v3.0 §5):
- Starter:
  - stamps/points and discount cards (`campaigns`);
  - automatic notices, a message to everyone, the basic "Te extrañamos" reminder
    (`basic_notifications`);
  - second-visit rate and clients per level (`reports`);
  - team (`user_management`);
  - "Estás cerca" (`nearby_notifications`, every plan since v3.0 decision 8).
- Growth adds group messages, events, flash offers, links and the 48 h result (`manual_campaigns`).
- Pro adds:
  - custom-text reminders and sequences, birthdays, yearly repeating campaigns, the level-up
    notice (`automated_campaigns`);
  - per-branch reports (`branch_reports`);
  - manager role (`staff_roles`);
  - export (`data_export`).

Money is `Decimal` end to end and is rounded to cents only at the final figure. The per-card
rates have three decimals (0.044), so rounding per line would drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from conpass_common.errors import FeatureNotInPlan, NotFound

CENT = Decimal("0.01")
IVA_RATE = Decimal("0.15")               # Ecuador IVA

LAUNCH = "launch"
LIST = "list"
PRICING_PHASES = (LAUNCH, LIST)
MONTHLY = "monthly"
ANNUAL = "annual"
BILLING_PERIODS = (MONTHLY, ANNUAL)

PAUSE_AFTER_DAYS = 90                    # v3.0 §4 / O2: same for every merchant, never configurable

# Feature keys (mirror the contract's PlanFeature enum).
CAMPAIGNS = "campaigns"                          # stamps / points / discount cards, every plan
BASIC_NOTIFICATIONS = "basic_notifications"      # automatic notices + message to all, every plan
REPORTS = "reports"                              # second visit, per campaign, clients per level
USER_MANAGEMENT = "user_management"              # cashiers (unlimited)
NEARBY_NOTIFICATIONS = "nearby_notifications"    # "Estás cerca" (every plan since v3.0)
MANUAL_CAMPAIGNS = "manual_campaigns"            # groups, events, flash offers, links, 48 h result
AUTOMATED_CAMPAIGNS = "automated_campaigns"      # custom texts, sequences, birthdays, level-up
BRANCH_REPORTS = "branch_reports"                # reports by branch
STAFF_ROLES = "staff_roles"                      # manager role
DATA_EXPORT = "data_export"                      # CSV / Excel export


@dataclass(frozen=True)
class Plan:
    tier: str
    launch_monthly_usd: Decimal          # IVA included
    list_monthly_usd: Decimal
    launch_annual_usd: Decimal
    list_annual_usd: Decimal
    included_cards: int
    extra_card_usd: Decimal              # + IVA
    included_notifications: int
    extra_notification_usd: Decimal      # + IVA
    location_limit: int                  # branches
    features: tuple[str, ...]
    operation_user_limit: int | None = None   # None = unlimited (every plan since v3.0)

    def has(self, feature: str) -> bool:
        return feature in self.features

    def monthly_usd(self, phase: str = LAUNCH) -> Decimal:
        return self.list_monthly_usd if phase == LIST else self.launch_monthly_usd

    def annual_usd(self, phase: str = LAUNCH) -> Decimal:
        return self.list_annual_usd if phase == LIST else self.launch_annual_usd

    def price_for(self, period: str, phase: str = LAUNCH) -> Decimal:
        return self.annual_usd(phase) if period == ANNUAL else self.monthly_usd(phase)

    @property
    def price_usd(self) -> Decimal:
        """Monthly launch price. Kept for callers that predate the pricing phase; new code
        uses `monthly_usd(phase)` / `subscription_price_usd`."""
        return self.launch_monthly_usd


_STARTER_FEATURES = (CAMPAIGNS, BASIC_NOTIFICATIONS, REPORTS, USER_MANAGEMENT,
                     NEARBY_NOTIFICATIONS)
_GROWTH_FEATURES = _STARTER_FEATURES + (MANUAL_CAMPAIGNS,)
_PRO_FEATURES = _GROWTH_FEATURES + (AUTOMATED_CAMPAIGNS, BRANCH_REPORTS, STAFF_ROLES,
                                    DATA_EXPORT)

PLANS: dict[str, Plan] = {
    "starter": Plan(
        tier="starter",
        launch_monthly_usd=Decimal("5.99"), list_monthly_usd=Decimal("7.99"),
        launch_annual_usd=Decimal("56.99"), list_annual_usd=Decimal("75.99"),
        included_cards=200, extra_card_usd=Decimal("0.044"),
        included_notifications=500, extra_notification_usd=Decimal("0.015"),
        location_limit=1, features=_STARTER_FEATURES,
    ),
    "growth": Plan(
        tier="growth",
        launch_monthly_usd=Decimal("17.99"), list_monthly_usd=Decimal("22.99"),
        launch_annual_usd=Decimal("171.99"), list_annual_usd=Decimal("219.99"),
        included_cards=1000, extra_card_usd=Decimal("0.034"),
        included_notifications=3000, extra_notification_usd=Decimal("0.010"),
        location_limit=3, features=_GROWTH_FEATURES,
    ),
    "pro": Plan(
        tier="pro",
        launch_monthly_usd=Decimal("57.99"), list_monthly_usd=Decimal("74.99"),
        launch_annual_usd=Decimal("555.99"), list_annual_usd=Decimal("718.99"),
        included_cards=5000, extra_card_usd=Decimal("0.024"),
        included_notifications=10000, extra_notification_usd=Decimal("0.005"),
        location_limit=10, features=_PRO_FEATURES,
    ),
}

TIER_ORDER: tuple[str, ...] = ("starter", "growth", "pro")


def get_plan(tier: str) -> Plan:
    try:
        return PLANS[tier]
    except KeyError:
        raise ValueError(f"unknown tier {tier!r}") from None


def cheapest_with(feature: str) -> str:
    """The lowest tier that includes `feature`: what an upgrade prompt should name."""
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


def next_tier_with_more_locations(tier: str) -> str | None:
    """The next tier up with a higher branch limit (None = already the top)."""
    current = get_plan(tier).location_limit
    for candidate in TIER_ORDER[TIER_ORDER.index(tier) + 1:]:
        if PLANS[candidate].location_limit > current:
            return candidate
    return None


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def split_iva(gross: Decimal) -> tuple[Decimal, Decimal]:
    """An IVA-inclusive amount → (net, IVA), each rounded to cents and summing to `gross`.
    $5.99 → ($5.21, $0.78); $7.99 → ($6.95, $1.04) (model v3.0 §4 invoice example)."""
    gross = money(gross)
    net = money(gross / (1 + IVA_RATE))
    return net, gross - net


def subscription_price_usd(sub: dict, phase: str = LAUNCH) -> Decimal:
    """What this subscription costs per billing period, IVA included. An annual term with a
    locked price keeps it until renewal (O1); otherwise the catalog price for the phase."""
    plan = get_plan(sub["tier"])
    period = sub.get("billing_period") or MONTHLY
    locked = sub.get("locked_price_usd")
    if period == ANNUAL and locked is not None:
        return money(Decimal(str(locked)))
    return plan.price_for(period, phase)


@dataclass(frozen=True)
class Estimate:
    subscription_usd: Decimal            # IVA included (0 in an annual term's non-renewal month)
    subscription_net_usd: Decimal
    subscription_iva_usd: Decimal
    extra_cards: int
    extra_cards_usd: Decimal             # net
    extra_notifications: int
    extra_notifications_usd: Decimal     # net
    overage_iva_usd: Decimal
    total_usd: Decimal                   # subscription + overage + IVA on overage

    @property
    def iva_usd(self) -> Decimal:
        return self.subscription_iva_usd + self.overage_iva_usd


def estimate(plan: Plan, active_cards: int, notifications_used: int = 0, *,
             subscription_usd: Decimal | None = None, phase: str = LAUNCH) -> Estimate:
    """One month's charge: the subscription (IVA incl.) plus overage plus IVA on the overage.

    `subscription_usd` defaults to the plan's monthly price for `phase`. An annual merchant
    prepaid its term, so the caller passes 0 outside the renewal month and only the overage
    is billed that month.

    Model v3.0 checks:
    - Starter with 750 notifications → $10.30;
    - Pro with 6,000 cards → $85.59;
    - Starter with 400 cards → $16.11.
    """
    sub = plan.monthly_usd(phase) if subscription_usd is None else money(subscription_usd)
    sub_net, sub_iva = split_iva(sub)
    extra_cards = max(0, active_cards - plan.included_cards)
    cards_usd = plan.extra_card_usd * extra_cards
    extra_notifs = max(0, notifications_used - plan.included_notifications)
    notifs_usd = plan.extra_notification_usd * extra_notifs
    overage_net = cards_usd + notifs_usd
    overage_iva = overage_net * IVA_RATE
    return Estimate(
        subscription_usd=sub,
        subscription_net_usd=sub_net,
        subscription_iva_usd=sub_iva,
        extra_cards=extra_cards,
        extra_cards_usd=money(cards_usd),
        extra_notifications=extra_notifs,
        extra_notifications_usd=money(notifs_usd),
        overage_iva_usd=money(overage_iva),
        total_usd=money(sub + overage_net + overage_iva),
    )


def period_start(today: date) -> date:
    """Usage month = calendar month (UTC)."""
    return today.replace(day=1)


def current_pricing_phase(client) -> str:
    """platform_settings.pricing_phase. 'launch' when the row is missing."""
    rows = (client.table("platform_settings").select("pricing_phase")
            .eq("id", True).execute().data)
    phase = rows[0]["pricing_phase"] if rows else LAUNCH
    return phase if phase in PRICING_PHASES else LAUNCH


def plan_to_api(plan: Plan, phase: str = LAUNCH) -> dict:
    """Plan → contract `Plan` (camelCase). `priceUsd` is what a new monthly subscriber pays
    today. The list price differs from it only during the launch phase, when the PWA shows it
    struck through."""
    out = {
        "tier": plan.tier,
        "priceUsd": float(plan.monthly_usd(phase)),
        "listPriceUsd": float(plan.list_monthly_usd),
        "annualPriceUsd": float(plan.annual_usd(phase)),
        "annualListPriceUsd": float(plan.list_annual_usd),
        "pricingPhase": phase,
        "includedCards": plan.included_cards,
        "extraCardUsd": float(plan.extra_card_usd),
        "includedNotifications": plan.included_notifications,
        "extraNotificationUsd": float(plan.extra_notification_usd),
        "locationLimit": plan.location_limit,
        "features": list(plan.features),
    }
    if plan.operation_user_limit is not None:
        out["operationUserLimit"] = plan.operation_user_limit
    return out


def next_tier_with_more_operation_users(tier: str) -> str | None:
    """The next tier up that allows more operation users than `tier` (None = none does).
    Every plan is unlimited since v3.0, so this is always None. It is kept for the add-user path."""
    current = get_plan(tier).operation_user_limit
    if current is None:
        return None
    for candidate in TIER_ORDER[TIER_ORDER.index(tier) + 1:]:
        limit = PLANS[candidate].operation_user_limit
        if limit is None or limit > current:
            return candidate
    return None


def subscription_to_api(sub: dict, active_cards: int | None = None,
                        phase: str = LAUNCH) -> dict:
    """`subscriptions` row → contract `Subscription`, derived from the catalog.

    The row stores the tier, payment state, billing period and an annual price lock; price,
    inclusion and limits come from `PLANS`. Shared by merchants and admin so the two Lambdas
    cannot drift. `activeCards` is included only when the caller has the count.
    """
    plan = get_plan(sub["tier"])
    period = sub.get("billing_period") or MONTHLY
    price = subscription_price_usd(sub, phase)
    out = {
        "tier": plan.tier,
        "paymentStatus": sub["payment_status"],
        "billingPeriod": period,
        "priceUsd": float(price),
        "listPriceUsd": float(plan.list_annual_usd if period == ANNUAL else plan.list_monthly_usd),
        "priceLocked": period == ANNUAL and sub.get("locked_price_usd") is not None,
        "includedCards": plan.included_cards,
        "includedNotifications": plan.included_notifications,
        "locationLimit": plan.location_limit,
        "nextChargeAt": sub.get("next_charge_at"),
        "lastPaymentAt": sub.get("last_payment_at"),
    }
    if sub.get("rejection_reason"):
        out["rejectionReason"] = sub["rejection_reason"]
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
