"""Automations — event (after an accrual) and time-based (daily) — Phase 16.

The decisions are PURE functions (`choose_event`, `plan_daily`, the window predicates) so
they are unit-tested without a database; `after_accrual` and `run_daily` wrap them with the
reads/writes. Design: docs/design/NOTIFICATIONS.md ("Automations").
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

from ..campaigns import ECUADOR_TZ
from ..demo import is_demo_merchant
from ..plans import AUTOMATED_CAMPAIGNS, get_plan, tier_for_merchant
from . import outbox, templates
from .settings import MerchantNotificationSettings, load_merchant_settings

log = logging.getLogger(__name__)

ACCRUE_KINDS = ("accrue_stamps", "accrue_points")
INACTIVE_WINDOW_DAYS = 7          # a reminder is only sent in the week after the threshold


# --------------------------------------------------------------------------- #
# Event automations (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccrualEvent:
    """What one committed accrual did to a card (all values AFTER-commit unless named)."""

    card_id: str
    merchant_id: str
    mechanic: str                 # "stamps" | "points"
    before_held: int              # stamps/points before the visit
    after_held: int
    gained: int                   # stamps or points added by this visit
    target: int | None            # stamps_for_reward / points_for_reward
    rewards_earned: int
    accrual_count: int            # the card's accruals INCLUDING this one


def _near_threshold(s: MerchantNotificationSettings, mechanic: str) -> int:
    return s.near_stamps if mechanic == templates.STAMPS else s.near_points


def crossed_near(ev: AccrualEvent, threshold: int) -> bool:
    """True when this visit brought the card from above the threshold to within it
    (`missing` in 1..threshold) without completing a reward — so it fires once, not on
    every later visit."""
    if ev.rewards_earned > 0 or not ev.target:
        return False
    missing_after = ev.target - ev.after_held
    missing_before = ev.target - ev.before_held
    return 1 <= missing_after <= threshold and missing_before > threshold


def choose_event(ev: AccrualEvent, s: MerchantNotificationSettings) -> str | None:
    """The ONE event automation an accrual fires: ready > milestone > near > balance,
    skipping any the merchant turned off. None when nothing applies."""
    if s.ready_on and ev.rewards_earned >= 1:
        return "ready"
    if s.milestone_on and s.milestone_every > 0 and ev.accrual_count % s.milestone_every == 0:
        return "milestone"
    if s.near_on and crossed_near(ev, _near_threshold(s, ev.mechanic)):
        return "near"
    if s.balance_on and ev.target:
        return "balance"
    return None


def event_facts(kind: str, ev: AccrualEvent, merchant: str,
                reward: str | None) -> templates.MessageFacts:
    return templates.MessageFacts(
        mechanic=ev.mechanic, merchant=merchant, reward=reward,
        n=ev.accrual_count if kind == "milestone" else ev.gained,
        held=ev.after_held, target=ev.target,
        missing=max(0, (ev.target or 0) - ev.after_held))


# --------------------------------------------------------------------------- #
# Event automations (I/O)
# --------------------------------------------------------------------------- #
def accrual_count(client, card_id: str) -> int:
    res = (client.table("transactions").select("id", count="exact")
           .eq("card_id", card_id).in_("kind", list(ACCRUE_KINDS)).execute())
    return res.count if res.count is not None else len(res.data or [])


def after_accrual(client, ev: AccrualEvent, *, today: date | None = None) -> str | None:
    """Evaluate and enqueue the event automation for a just-committed accrual.

    Returns the chosen automation id (None when nothing fired). Callers treat this as
    best-effort and catch everything: it must never fail the cashier's operation.
    """
    if is_demo_merchant(client, ev.merchant_id):
        return None           # the public demo never automates: it would be a spam channel
    s = load_merchant_settings(client, ev.merchant_id)
    ev = _with_count(client, ev)
    kind = choose_event(ev, s)
    if kind is None:
        return None
    card = (client.table("cards").select("language,program_id")
            .eq("id", ev.card_id).limit(1).execute().data or [None])[0]
    if card is None:
        return None
    program = (client.table("programs").select("reward").eq("id", card["program_id"])
               .limit(1).execute().data or [{}])[0]
    merchant = (client.table("merchants").select("business_name")
                .eq("id", ev.merchant_id).limit(1).execute().data or [{}])[0]
    name = merchant.get("business_name") or ""
    body = templates.render(kind, card.get("language"),
                            event_facts(kind, ev, name, program.get("reward")))
    outbox.enqueue_event(client, merchant_id=ev.merchant_id, card_id=ev.card_id, type_=kind,
                         header=name, body=body, day=today or datetime.now(ECUADOR_TZ).date())
    return kind


def _with_count(client, ev: AccrualEvent) -> AccrualEvent:
    if ev.accrual_count > 0:
        return ev
    return replace(ev, accrual_count=accrual_count(client, ev.card_id))


# --------------------------------------------------------------------------- #
# Time-based automations (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CardInfo:
    card_id: str
    program_type: str
    stamps: int
    points: int
    target: int | None
    last_at: datetime
    language: str = "es"
    reward: str | None = None

    @property
    def mechanic(self) -> str:
        return templates.STAMPS if self.program_type == "loyalty_stamps" else templates.POINTS

    @property
    def held(self) -> int:
        return self.stamps if self.mechanic == templates.STAMPS else self.points


def parse_ts(value: str | datetime) -> datetime:
    """A PostgREST timestamp as an aware UTC datetime."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_expired(last_at: datetime, now: datetime, expiry_days: int) -> bool:
    return last_at <= now - timedelta(days=expiry_days)


def in_expiry_warning_window(last_at: datetime, now: datetime, expiry_days: int,
                             warn_days: int) -> bool:
    """now - N < last_at <= now - (N - warn): not yet expired, within `warn` days of it."""
    return now - timedelta(days=expiry_days) < last_at <= now - timedelta(
        days=expiry_days - warn_days)


def days_until_expiry(last_at: datetime, now: datetime, expiry_days: int) -> int:
    """Whole days left, rounded up, never below 1 (the card is inside the warn window)."""
    left = (last_at + timedelta(days=expiry_days) - now).total_seconds() / 86400
    return max(1, math.ceil(left))


def in_inactive_window(last_at: datetime, now: datetime, days: int) -> bool:
    """now - days - 7d < last_at <= now - days: one week of eligibility, then silence."""
    return (now - timedelta(days=days + INACTIVE_WINDOW_DAYS) < last_at
            <= now - timedelta(days=days))


def dedupe_key(kind: str, card_id: str, last_at: datetime) -> str:
    return f"{kind}:{card_id}:{last_at.astimezone(UTC).date().isoformat()}"


@dataclass
class DailyPlan:
    expire: list[CardInfo] = field(default_factory=list)
    warn: list[tuple[CardInfo, int]] = field(default_factory=list)       # (card, days left)
    inactive: list[tuple[CardInfo, int]] = field(default_factory=list)   # (card, days since)


def plan_daily(cards: list[CardInfo], s: MerchantNotificationSettings, now: datetime, *,
               inactive_allowed: bool) -> DailyPlan:
    """Which cards expire / get an expiry warning / an inactivity reminder today.

    Expiry is exclusive with its warning (an expired card has nothing left to warn about).
    Only cards holding a balance are touched or messaged — nothing to lose, nothing to say.
    """
    plan = DailyPlan()
    n = s.balance_expiry_days
    for c in cards:
        if c.held <= 0:
            continue
        if n is not None and is_expired(c.last_at, now, n):
            plan.expire.append(c)
            continue
        if (n is not None and s.expiry_on
                and in_expiry_warning_window(c.last_at, now, n, s.expiry_warn_days)):
            plan.warn.append((c, days_until_expiry(c.last_at, now, n)))
        if (inactive_allowed and s.inactive_on
                and in_inactive_window(c.last_at, now, s.inactive_days)):
            plan.inactive.append((c, max(1, (now - c.last_at).days)))
    return plan


# --------------------------------------------------------------------------- #
# Time-based automations (I/O)
# --------------------------------------------------------------------------- #
def load_card_infos(client, merchant_id: str) -> list[CardInfo]:
    """Notifiable cards of a merchant with their last activity, language and reward."""
    cards = outbox.fetch_all(lambda: client.table("notifiable_cards").select(
        "card_id,program_id,program_type,stamps,points,stamps_for_reward,points_for_reward")
        .eq("merchant_id", merchant_id).order("card_id"))
    activity = {r["card_id"]: r["last_at"] for r in outbox.fetch_all(
        lambda: client.table("card_last_activity").select("card_id,last_at")
        .eq("merchant_id", merchant_id).order("card_id"))}
    langs = {r["id"]: r.get("language") for r in outbox.fetch_all(
        lambda: client.table("cards").select("id,language")
        .eq("merchant_id", merchant_id).order("id"))}
    rewards = {r["id"]: r.get("reward") for r in
               client.table("programs").select("id,reward").eq(
                   "merchant_id", merchant_id).execute().data or []}
    out = []
    for c in cards:
        if c["card_id"] not in activity:
            continue
        stamps_mech = c["program_type"] == "loyalty_stamps"
        out.append(CardInfo(
            card_id=c["card_id"], program_type=c["program_type"], stamps=c["stamps"],
            points=c["points"],
            target=c["stamps_for_reward"] if stamps_mech else c["points_for_reward"],
            last_at=parse_ts(activity[c["card_id"]]), language=langs.get(c["card_id"]) or "es",
            reward=rewards.get(c["program_id"])))
    return out


def expire_balance(client, card: CardInfo, now: datetime, *, push) -> bool:
    """Zero a card's stamps/points (rewards earned are kept), audit it, refresh the pass.

    The update is guarded on the balance we read so a visit that landed in between wins.
    """
    updated = (client.table("cards").update({"stamps": 0, "points": 0})
               .eq("id", card.card_id).eq("stamps", card.stamps).eq("points", card.points)
               .execute().data or [])
    if not updated:
        return False
    merchant_id = updated[0]["merchant_id"]
    client.table("balance_expirations").insert({
        "card_id": card.card_id, "merchant_id": merchant_id,
        "stamps_expired": card.stamps, "points_expired": card.points,
        "last_activity_at": card.last_at.isoformat(), "expired_at": now.isoformat()}).execute()
    try:
        push(client, card.card_id)
    except Exception:  # noqa: BLE001 - the pass refresh is best-effort
        log.warning("push after expiry failed for card %s", card.card_id, exc_info=True)
    return True


def _timed_rows(kind: str, items: list[tuple[CardInfo, int]], merchant: str) -> list[dict]:
    rows = []
    for c, days in items:
        facts = templates.MessageFacts(mechanic=c.mechanic, merchant=merchant, reward=c.reward,
                                       held=c.held, target=c.target, days=days)
        rows.append({"card_id": c.card_id, "body": templates.render(kind, c.language, facts),
                     "dedupe_key": dedupe_key(kind, c.card_id, c.last_at)})
    return rows


def run_daily_for_merchant(client, merchant_id: str, s: MerchantNotificationSettings,
                           now: datetime, *, push) -> dict:
    """Expire balances, then enqueue expiry warnings and inactivity reminders."""
    try:
        inactive_allowed = get_plan(tier_for_merchant(client, merchant_id)).has(
            AUTOMATED_CAMPAIGNS)
    except Exception:  # noqa: BLE001 - no subscription row: no Pro-only automation
        inactive_allowed = False
    plan = plan_daily(load_card_infos(client, merchant_id), s, now,
                      inactive_allowed=inactive_allowed)
    expired = sum(1 for c in plan.expire if expire_balance(client, c, now, push=push))
    name = ((client.table("merchants").select("business_name").eq("id", merchant_id)
             .limit(1).execute().data or [{}])[0].get("business_name") or "")
    day = now.astimezone(ECUADOR_TZ).date()
    warned = outbox.enqueue_timed(client, merchant_id=merchant_id, type_="expiry", day=day,
                                  header=name, rows=_timed_rows("expiry", plan.warn, name))
    nudged = outbox.enqueue_timed(client, merchant_id=merchant_id, type_="inactive", day=day,
                                  header=name, rows=_timed_rows("inactive", plan.inactive, name))
    return {"expired": expired, "warned": warned, "inactive": nudged}


def run_daily(client, now: datetime | None = None, *, push=None) -> dict:
    """The 08:00 Ecuador job over every merchant with a stored settings row that has
    something time-based switched on (no row = defaults = nothing to do: expiry window
    unset, inactivity off). One merchant failing never stops the others."""
    from ..wallet_sync import push_card
    push = push or push_card
    now = now or datetime.now(UTC)
    totals = {"merchants": 0, "expired": 0, "warned": 0, "inactive": 0, "errors": 0}
    rows = outbox.fetch_all(lambda: client.table("merchant_notification_settings")
                            .select("*").order("merchant_id"))
    for row in rows:
        s = MerchantNotificationSettings(**{k: row[k] for k in
                                            MerchantNotificationSettings.__dataclass_fields__
                                            if k in row})
        if s.balance_expiry_days is None and not s.inactive_on:
            continue
        try:
            if is_demo_merchant(client, row["merchant_id"]):
                continue      # demo: no expiry warnings, reminders or balance zeroing

            result = run_daily_for_merchant(client, row["merchant_id"], s, now, push=push)
        except Exception:  # noqa: BLE001 - isolate the merchant
            totals["errors"] += 1
            log.exception("daily automations failed for merchant %s", row["merchant_id"])
            continue
        totals["merchants"] += 1
        for k, v in result.items():
            totals[k] += v
    return totals
