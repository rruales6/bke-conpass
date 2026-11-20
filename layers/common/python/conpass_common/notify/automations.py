"""Automations — event (after an accrual / a redemption), time-based (daily) and birthday.

The decisions are PURE functions (`choose_event`, `plan_daily`, the window predicates) so
they are unit-tested without a database; `after_accrual`, `after_redemption`, `run_daily` and
`run_birthdays` wrap them with the reads/writes. Design: docs/design/NOTIFICATIONS.md and
docs/design/PHASE-18-CONSOLE.md (v3.0):

- event: ready > near > balance after an accrual (balance is off by default); "Subiste a nivel"
  (Pro) after a redemption. Milestone and balance expiry are retired (O2).
- daily: "Te extrañamos" (every plan) at `days` without use, replaced by the Pro sequence
  while that is on; both are sent once per absence (dedupe key on the last use).
- birthday (Pro, 09:00 Ecuador): a birthday reward on the customer's most recently used card.
- Pro automations check the plan at RUN time; demo merchants get none.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..campaigns import ECUADOR_TZ
from ..demo import is_demo_merchant
from . import outbox, templates
from .settings import (
    MerchantNotificationSettings,
    is_pro,
    load_merchant_settings,
    settings_from_row,
)

log = logging.getLogger(__name__)

ACCRUE_KINDS = ("accrue_stamps", "accrue_points")
ACTIVITY_KINDS = ("accrue_stamps", "accrue_points", "redeem")
LOYALTY_TYPES = ("loyalty_stamps", "loyalty_points")
INACTIVE_WINDOW_DAYS = 7          # a reminder is only sent in the week after the threshold
STATS_DAYS = 30
VISIT_WINDOW = timedelta(hours=48)
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L: read aloud at the till
CODE_TRIES = 5
STAT_KEYS = {"ready": "ready", "near": "near", "inactive": "inactive", "sequence": "sequence",
             "level_up": "levelUp", "birthday": "birthday", "balance": "balance"}


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
    accrual_count: int = 0        # unused since the milestone automation was retired


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
    """The ONE event automation an accrual fires: ready > near > balance, skipping any the
    merchant turned off. None when nothing applies."""
    if s.ready_on and ev.rewards_earned >= 1:
        return "ready"
    if s.near_on and crossed_near(ev, _near_threshold(s, ev.mechanic)):
        return "near"
    if s.balance_on and ev.target:
        return "balance"
    return None


def event_facts(kind: str, ev: AccrualEvent, merchant: str,
                reward: str | None) -> templates.MessageFacts:
    return templates.MessageFacts(
        mechanic=ev.mechanic, merchant=merchant, reward=reward, n=ev.gained,
        held=ev.after_held, target=ev.target,
        missing=max(0, (ev.target or 0) - ev.after_held))


# --------------------------------------------------------------------------- #
# Event automations (I/O)
# --------------------------------------------------------------------------- #
def _facts_context(client, card_id: str, merchant_id: str,
                   program_id: str | None = None) -> tuple[str | None, str | None, str] | None:
    """(language, reward, business name) for a card's message; None when the card is gone."""
    card = (client.table("cards").select("language,program_id")
            .eq("id", card_id).limit(1).execute().data or [None])[0]
    if card is None:
        return None
    program = (client.table("programs").select("reward").eq("id", program_id or card["program_id"])
               .limit(1).execute().data or [{}])[0]
    merchant = (client.table("merchants").select("business_name").eq("id", merchant_id)
                .limit(1).execute().data or [{}])[0]
    return card.get("language"), program.get("reward"), merchant.get("business_name") or ""


def after_accrual(client, ev: AccrualEvent, *, today: date | None = None) -> str | None:
    """Evaluate and enqueue the event automation for a just-committed accrual.

    Returns the chosen automation id (None when nothing fired). Callers treat this as
    best-effort and catch everything: it must never fail the cashier's operation.
    """
    if is_demo_merchant(client, ev.merchant_id):
        return None           # the public demo never automates: it would be a spam channel
    s = load_merchant_settings(client, ev.merchant_id)
    kind = choose_event(ev, s)
    if kind is None:
        return None
    ctx = _facts_context(client, ev.card_id, ev.merchant_id)
    if ctx is None:
        return None
    language, reward, name = ctx
    custom = s.custom_text(kind) if kind in ("ready", "near") else None
    if custom and not is_pro(client, ev.merchant_id):
        custom = None         # custom wording is Pro: a downgraded merchant gets the fixed text
    body = templates.render(kind, language, event_facts(kind, ev, name, reward), custom=custom)
    outbox.enqueue_event(client, merchant_id=ev.merchant_id, card_id=ev.card_id, type_=kind,
                         header=name, body=body, day=today or datetime.now(ECUADOR_TZ).date())
    return kind


def after_redemption(client, *, card_id: str, merchant_id: str, program_id: str,
                     new_level: int, today: date | None = None) -> str | None:
    """The "Subiste a nivel" automation (Pro, levelUp enabled), fired after a committed
    redemption raised the card's level to `new_level`. Queues one 'level_up' delivery for the
    card through the same event path as `after_accrual`. Returns 'level_up' or None.
    Never raises: the redemption has already committed."""
    try:
        if is_demo_merchant(client, merchant_id):
            return None
        s = load_merchant_settings(client, merchant_id)
        if not s.level_up_on or not is_pro(client, merchant_id):
            return None
        ctx = _facts_context(client, card_id, merchant_id, program_id)
        if ctx is None:
            return None
        language, reward, name = ctx
        facts = templates.MessageFacts(merchant=name, reward=reward, level=new_level)
        body = templates.render("level_up", language, facts, custom=s.custom_text("level_up"))
        outbox.enqueue_event(client, merchant_id=merchant_id, card_id=card_id,
                             type_="level_up", header=name, body=body,
                             day=today or datetime.now(ECUADOR_TZ).date())
        return "level_up"
    except Exception:  # noqa: BLE001 - best-effort by contract
        log.warning("level-up notice failed for card %s", card_id, exc_info=True)
        return None


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
    level: int | None = None

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


def in_inactive_window(last_at: datetime, now: datetime, days: int) -> bool:
    """now - days - 7d < last_at <= now - days: one week of eligibility, then silence."""
    return (now - timedelta(days=days + INACTIVE_WINDOW_DAYS) < last_at
            <= now - timedelta(days=days))


def dedupe_key(kind: str, card_id: str, last_at: datetime, step: int | None = None) -> str:
    """One reminder per absence: keyed on the card's last use (which moves when the customer
    returns, so the next absence gets a fresh key) and, for the sequence, the step."""
    key = f"{kind}:{card_id}:{last_at.astimezone(UTC).date().isoformat()}"
    return key if step is None else f"{key}:{step}"


def sequence_steps(s: MerchantNotificationSettings) -> list[tuple[int, str]]:
    """The stored steps as (day, text), ascending and without repeated days."""
    steps: dict[int, str] = {}
    for step in s.sequence_steps or []:
        try:
            steps.setdefault(int(step["day"]), str(step.get("text") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(steps.items())[:3]


@dataclass
class DailyPlan:
    inactive: list[tuple[CardInfo, int]] = field(default_factory=list)   # (card, days since)
    # (card, step index, step text, days since)
    sequence: list[tuple[CardInfo, int, str, int]] = field(default_factory=list)


def plan_daily(cards: list[CardInfo], s: MerchantNotificationSettings, now: datetime, *,
               sequence_active: bool = False) -> DailyPlan:
    """Which cards get "Te extrañamos" / a sequence step today.

    `sequence_active` (the Pro sequence is on AND the merchant is still Pro) replaces the
    single reminder. A step covers the days from its own day up to the next step's day (the
    last one a week), so a card gets only the latest step it has reached, never a burst.
    """
    plan = DailyPlan()
    steps = sequence_steps(s) if sequence_active else []
    for c in cards:
        age = now - c.last_at
        days = max(1, age.days)
        if sequence_active:
            for i, (day, text) in enumerate(steps):
                upper = steps[i + 1][0] if i + 1 < len(steps) else day + INACTIVE_WINDOW_DAYS
                if timedelta(days=day) <= age < timedelta(days=upper):
                    plan.sequence.append((c, i, text, days))
        elif s.inactive_on and in_inactive_window(c.last_at, now, s.inactive_days):
            plan.inactive.append((c, days))
    return plan


# --------------------------------------------------------------------------- #
# Time-based automations (I/O)
# --------------------------------------------------------------------------- #
def _merchant_name(client, merchant_id: str) -> str:
    return ((client.table("merchants").select("business_name").eq("id", merchant_id)
             .limit(1).execute().data or [{}])[0].get("business_name") or "")


def load_card_infos(client, merchant_id: str) -> list[CardInfo]:
    """Notifiable loyalty cards of a merchant with their last activity, language and reward."""
    cards = outbox.fetch_all(lambda: client.table("notifiable_cards").select(
        "card_id,program_id,program_type,stamps,points,stamps_for_reward,points_for_reward,level")
        .eq("merchant_id", merchant_id).in_("program_type", list(LOYALTY_TYPES))
        .order("card_id"))
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
            reward=rewards.get(c["program_id"]), level=c.get("level")))
    return out


def _timed_facts(c: CardInfo, merchant: str, days: int) -> templates.MessageFacts:
    held = c.held
    return templates.MessageFacts(
        mechanic=c.mechanic, merchant=merchant, reward=c.reward, held=held, target=c.target,
        missing=max(0, (c.target or 0) - held), days=days, level=c.level)


def _inactive_rows(items: list[tuple[CardInfo, int]], merchant: str,
                   custom: str | None) -> list[dict]:
    return [{"card_id": c.card_id,
             "body": templates.render("inactive", c.language, _timed_facts(c, merchant, days),
                                      custom=custom),
             "dedupe_key": dedupe_key("inactive", c.card_id, c.last_at)}
            for c, days in items]


def _sequence_rows(items: list[tuple[CardInfo, int, str, int]], merchant: str) -> list[dict]:
    return [{"card_id": c.card_id,
             "body": templates.render("sequence", c.language, _timed_facts(c, merchant, days),
                                      custom=text),
             "dedupe_key": dedupe_key("sequence", c.card_id, c.last_at, step)}
            for c, step, text, days in items]


def run_daily_for_merchant(client, merchant_id: str, s: MerchantNotificationSettings,
                           now: datetime) -> dict:
    """Queue "Te extrañamos" or, while the Pro sequence is on, its steps."""
    pro = is_pro(client, merchant_id)
    sequence_active = s.sequence_on and pro
    if not s.inactive_on and not sequence_active:
        return {"inactive": 0, "sequence": 0}
    plan = plan_daily(load_card_infos(client, merchant_id), s, now,
                      sequence_active=sequence_active)
    name = _merchant_name(client, merchant_id)
    day = now.astimezone(ECUADOR_TZ).date()
    custom = s.custom_text("inactive") if pro else None
    nudged = outbox.enqueue_timed(
        client, merchant_id=merchant_id, type_="inactive", day=day, header=name, now=now,
        rows=_inactive_rows(plan.inactive, name, custom))
    stepped = outbox.enqueue_timed(
        client, merchant_id=merchant_id, type_="sequence", day=day, header=name, now=now,
        rows=_sequence_rows(plan.sequence, name))
    return {"inactive": nudged, "sequence": stepped}


def run_daily(client, now: datetime | None = None) -> dict:
    """The 08:00 Ecuador job: "Te extrañamos" (every plan, on by default, so every real
    merchant is visited, with or without a stored settings row) and the Pro sequence.
    Demo merchants are skipped. One merchant failing never stops the others."""
    now = now or datetime.now(UTC)
    totals = {"merchants": 0, "inactive": 0, "sequence": 0, "errors": 0}
    stored = {r["merchant_id"]: r for r in outbox.fetch_all(
        lambda: client.table("merchant_notification_settings").select("*")
        .order("merchant_id"))}
    merchants = outbox.fetch_all(lambda: client.table("merchants").select("id,is_demo")
                                 .order("id"))
    for m in merchants:
        if m.get("is_demo"):
            continue      # demo: no reminders
        s = settings_from_row(stored[m["id"]]) if m["id"] in stored else \
            MerchantNotificationSettings()
        try:
            result = run_daily_for_merchant(client, m["id"], s, now)
        except Exception:  # noqa: BLE001 - isolate the merchant
            totals["errors"] += 1
            log.exception("daily automations failed for merchant %s", m["id"])
            continue
        totals["merchants"] += 1
        for k, v in result.items():
            totals[k] += v
    return totals


# --------------------------------------------------------------------------- #
# Birthdays (Pro, 09:00 Ecuador)
# --------------------------------------------------------------------------- #
def birthday_days(today: date) -> tuple[int, list[int]]:
    """(month, days) of birthdays that fire on `today`. Feb 29 falls on Feb 28 in a
    non-leap year."""
    days = [today.day]
    if today.month == 2 and today.day == 28 and not _is_leap(today.year):
        days.append(29)
    return today.month, days


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def birthday_benefit(s: MerchantNotificationSettings) -> str | None:
    """The reward text ("Un postre" / "20 % de descuento"), None when misconfigured."""
    if s.birthday_benefit == "discount":
        pct = s.birthday_discount_percent
        return f"{pct} % de descuento" if pct else None
    gift = (s.birthday_gift or "").strip()
    return gift or None


def new_birthday_code() -> str:
    return "CUMPLE-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))


def _birthday_exists(client, customer_id: str, year: int) -> dict | None:
    rows = (client.table("birthday_cards").select("*").eq("customer_id", customer_id)
            .eq("year", year).limit(1).execute().data or [])
    return rows[0] if rows else None


def _issue_birthday_card(client, *, merchant_id: str, customer_id: str, card: dict,
                         benefit: str, valid_until: date, year: int) -> dict | None:
    """Insert the reward; None when another run already issued this year's (unique index on
    (customer_id, year)). Retries a code collision."""
    for _ in range(CODE_TRIES):
        try:
            return client.table("birthday_cards").insert({
                "program_id": card["program_id"], "merchant_id": merchant_id,
                "customer_id": customer_id, "card_id": card["card_id"], "year": year,
                "code": new_birthday_code(), "benefit": benefit,
                "valid_until": valid_until.isoformat(), "status": "issued"}).execute().data[0]
        except Exception:  # noqa: BLE001 - unique violation: (customer, year) or the code
            if _birthday_exists(client, customer_id, year) is not None:
                return None
    raise RuntimeError("could not generate a unique birthday code")


def _birthday_cards_of(client, customer_ids: list[str]) -> dict[str, dict]:
    """customer_id -> their most recently used notifiable loyalty card."""
    cards: list[dict] = []
    for ids in outbox.chunks(customer_ids, 100):
        cards.extend(client.table("cards").select("id,customer_id,language,program_id")
                     .in_("customer_id", ids).execute().data or [])
    by_id = {c["id"]: c for c in cards}
    live: dict[str, dict] = {}
    activity: dict[str, str] = {}
    for ids in outbox.chunks(list(by_id), 100):
        for r in (client.table("notifiable_cards").select("card_id,program_id,program_type")
                  .in_("card_id", ids).execute().data or []):
            if r["program_type"] in LOYALTY_TYPES:
                live[r["card_id"]] = r
        for r in (client.table("card_last_activity").select("card_id,last_at")
                  .in_("card_id", ids).execute().data or []):
            activity[r["card_id"]] = r["last_at"]
    best: dict[str, dict] = {}
    for card_id, view in live.items():
        card = by_id[card_id]
        last = parse_ts(activity[card_id]) if card_id in activity else datetime.min.replace(
            tzinfo=UTC)
        cur = best.get(card["customer_id"])
        if cur is None or last > cur["last_at"]:
            best[card["customer_id"]] = {
                "card_id": card_id, "program_id": view["program_id"],
                "language": card.get("language"), "last_at": last}
    return best


def run_birthday_for_merchant(client, merchant_id: str, s: MerchantNotificationSettings,
                              now: datetime) -> dict:
    today = now.astimezone(ECUADOR_TZ).date()
    benefit = birthday_benefit(s)
    out = {"issued": 0, "queued": 0}
    if benefit is None:
        log.warning("birthday automation of merchant %s has no gift / discount", merchant_id)
        return out
    month, days = birthday_days(today)
    customers = [r["id"] for r in outbox.fetch_all(
        lambda: client.table("customers").select("id").eq("merchant_id", merchant_id)
        .eq("birthday_month", month).in_("birthday_day", days).order("id"))]
    if not customers:
        return out
    name = _merchant_name(client, merchant_id)
    valid_until = today + timedelta(days=s.birthday_validity_days - 1)
    prize = benefit if s.birthday_benefit == "gift" else f"un {benefit}"
    custom = s.custom_text("birthday")
    chosen = _birthday_cards_of(client, customers)
    rows: list[dict] = []
    for customer_id in customers:
        card = chosen.get(customer_id)
        if card is None:
            continue          # no active, saved loyalty card to attach it to
        existing = _birthday_exists(client, customer_id, today.year)
        if existing is None:
            issued = _issue_birthday_card(client, merchant_id=merchant_id,
                                          customer_id=customer_id, card=card, benefit=benefit,
                                          valid_until=valid_until, year=today.year)
            out["issued"] += 1 if issued else 0
            left = s.birthday_validity_days
        else:
            if existing.get("status") != "issued" or not existing.get("card_id"):
                continue
            card = {**card, "card_id": existing["card_id"]}
            left = max(1, (date.fromisoformat(existing["valid_until"]) - today).days + 1)
        facts = templates.MessageFacts(merchant=name, reward=prize, days=left)
        rows.append({"card_id": card["card_id"],
                     "body": templates.render("birthday", card["language"], facts, custom=custom),
                     "dedupe_key": f"birthday:{customer_id}:{today.year}"})
    out["queued"] = outbox.enqueue_timed(client, merchant_id=merchant_id, type_="birthday",
                                         day=today, header=name, rows=rows, now=now)
    return out


def run_birthdays(client, now: datetime | None = None) -> dict:
    """The 09:00 Ecuador job: for every Pro merchant with the birthday automation on, issue
    the year's birthday reward to customers whose birthday is today (Feb 29 -> Feb 28 in a
    non-leap year) and queue the notice. Idempotent: one reward per customer per year, one
    notice per reward. Demo merchants and merchants no longer on Pro are skipped."""
    now = now or datetime.now(UTC)
    totals = {"merchants": 0, "issued": 0, "queued": 0, "errors": 0}
    rows = outbox.fetch_all(lambda: client.table("merchant_notification_settings")
                            .select("*").eq("birthday_on", True).order("merchant_id"))
    for row in rows:
        merchant_id = row["merchant_id"]
        try:
            if is_demo_merchant(client, merchant_id) or not is_pro(client, merchant_id):
                continue
            result = run_birthday_for_merchant(client, merchant_id, settings_from_row(row), now)
        except Exception:  # noqa: BLE001 - isolate the merchant
            totals["errors"] += 1
            log.exception("birthday automation failed for merchant %s", merchant_id)
            continue
        totals["merchants"] += 1
        totals["issued"] += result["issued"]
        totals["queued"] += result["queued"]
    return totals


# --------------------------------------------------------------------------- #
# Result per automation (last 30 days)
# --------------------------------------------------------------------------- #
def automation_stats(client, merchant_id: str, now: datetime | None = None) -> dict[str, dict]:
    """`{key: {sent, visits48h}}` for the last 30 days, keys ready, near, inactive, sequence,
    levelUp, birthday, balance (absent = nothing sent).

    Three kinds of read, all batched: the merchant's automation batches (type per id), the
    `sent` deliveries of those batches, and the effective operations (the view that already
    drops undone ones and their reversals) of the cards reached. A visit is an effective
    accrual or redemption in (sent_at, sent_at + 48 h], as notification_history_view counts."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=STATS_DAYS)
    # A batch's day is when it was queued; a delivery can go out a little later (quiet hours).
    batches = outbox.fetch_all(lambda: client.table("notifications").select("id,type")
                               .eq("merchant_id", merchant_id).eq("source", "automation")
                               .gte("day", (since.astimezone(ECUADOR_TZ).date()
                                            - timedelta(days=3)).isoformat()).order("id"))
    type_of = {b["id"]: b["type"] for b in batches}
    sent: list[tuple[str, str, datetime]] = []
    for ids in outbox.chunks(list(type_of), 50):
        for d in outbox.fetch_all(lambda ids=ids: client.table("notification_deliveries")
                                  .select("id,notification_id,card_id,sent_at")
                                  .in_("notification_id", ids).eq("status", "sent")
                                  .gte("sent_at", since.isoformat()).order("id")):
            sent.append((type_of[d["notification_id"]], d["card_id"], parse_ts(d["sent_at"])))
    visits: dict[str, list[datetime]] = {}
    card_ids = list({c for _, c, _ in sent})
    first = min((t for _, _, t in sent), default=None)
    for ids in outbox.chunks(card_ids, 100):
        for r in outbox.fetch_all(lambda ids=ids: client.table("effective_transactions")
                                  .select("id,card_id,created_at").in_("card_id", ids)
                                  .in_("kind", list(ACTIVITY_KINDS))
                                  .gte("created_at", first.isoformat()).order("id")):
            visits.setdefault(r["card_id"], []).append(parse_ts(r["created_at"]))
    stats: dict[str, dict] = {}
    for type_, card_id, at in sent:
        key = STAT_KEYS.get(type_)
        if key is None:
            continue
        stat = stats.setdefault(key, {"sent": 0, "visits48h": 0})
        stat["sent"] += 1
        if any(at < t <= at + VISIT_WINDOW for t in visits.get(card_id, ())):
            stat["visits48h"] += 1
    return stats
