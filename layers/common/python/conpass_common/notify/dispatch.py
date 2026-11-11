"""The outbox dispatcher (Phase 16): claim due deliveries, apply the platform rules, send.

Decisions (`decide`, `retry_outcome`, quiet-hour helpers) are PURE with an injected clock;
`run_dispatch` does the I/O. Design: docs/design/NOTIFICATIONS.md ("Dispatcher").
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..campaigns import ECUADOR_TZ
from ..providers.wallet import PassMessage, WalletObjectMissing, WalletQuotaExceeded
from . import outbox
from .settings import QUIET_END_HOUR, QUIET_START_HOUR, PlatformRules, load_platform_rules

log = logging.getLogger(__name__)

BATCH = 200
MAX_WORKERS = 8
MAX_ATTEMPTS = 3
RETRY_STEP = timedelta(minutes=5)       # 5 min x attempt
CAP_WINDOW = timedelta(hours=24)
RUN_BUDGET_S = 240.0                    # Lambda timeout 300 s; stop claiming ~60 s before
CLOSE_GRACE = timedelta(minutes=2)      # a notification this young may still be enqueuing

SEND, DEFER, SKIP = "send", "defer", "skip"
AUTOMATION, MANUAL = "automation", "manual"


# --------------------------------------------------------------------------- #
# Pure decisions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Decision:
    action: str                          # send | defer | skip
    not_before: datetime | None = None   # defer
    reason: str | None = None            # skip / defer explanation


def in_quiet_hours(now: datetime) -> bool:
    """21:00 <= Ecuador time < 08:00."""
    hour = now.astimezone(ECUADOR_TZ).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def next_quiet_end(now: datetime) -> datetime:
    """The next 08:00 Ecuador strictly after `now`, as UTC."""
    local = now.astimezone(ECUADOR_TZ)
    end = local.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(UTC)


def cap_release_time(sent_times: list[datetime], cap: int) -> datetime:
    """When the card's rolling-24 h count drops below `cap`: the (len-cap+1)-th oldest
    send + 24 h (the oldest, when it is exactly at the cap)."""
    ordered = sorted(sent_times)
    return ordered[len(ordered) - cap] + CAP_WINDOW


def decide(*, now: datetime, source: str, notifiable: bool, rules: PlatformRules,
           sent_times: list[datetime], display_until: datetime | None = None) -> Decision:
    """What to do with one claimed delivery, in the design's order: no longer notifiable
    -> skip; quiet hours -> defer to 08:00; daily cap -> manual deferred, automation
    skipped; else send. `sent_times` = this card's push-`sent` times within the last 24 h."""
    if not notifiable:
        return Decision(SKIP, reason="not_notifiable")
    if display_until is not None and display_until <= now:
        return Decision(SKIP, reason="expired")
    if rules.quiet_hours_enabled and in_quiet_hours(now):
        return Decision(DEFER, next_quiet_end(now), "quiet_hours")
    recent = [t for t in sent_times if t > now - CAP_WINDOW]
    if len(recent) >= rules.cap_per_day:
        if source == MANUAL:
            return Decision(DEFER, cap_release_time(recent, rules.cap_per_day), "daily_cap")
        return Decision(SKIP, reason="daily_cap")
    return Decision(SEND)


def retry_outcome(attempts: int, now: datetime) -> tuple[str, datetime | None]:
    """After a failed send (`attempts` already counts it): back to pending in
    5 min x attempts, or `failed` once MAX_ATTEMPTS is used up."""
    if attempts >= MAX_ATTEMPTS:
        return "failed", None
    return "pending", now + RETRY_STEP * attempts


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _update(client, delivery_id: str, **fields) -> None:
    client.table("notification_deliveries").update(fields).eq("id", delivery_id).execute()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _ts(value) -> datetime | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def expand_due_scheduled(client, now: datetime) -> int:
    """Turn due `scheduled` notifications into outbox rows (same RPC as a send-now)."""
    due = (client.table("notifications").select("id").eq("status", "scheduled")
           .lte("scheduled_at", now.isoformat()).execute().data or [])
    for row in due:
        try:
            client.rpc("enqueue_manual_notification", {"p_notification": row["id"]}).execute()
        except Exception:  # noqa: BLE001 - one bad row must not block the rest
            log.exception("expanding scheduled notification %s failed", row["id"])
    return len(due)


def _sent_times(client, card_ids: list[str], now: datetime) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = defaultdict(list)
    since = (now - CAP_WINDOW).isoformat()
    for ids in outbox.chunks(card_ids, 100):
        rows = (client.table("notification_deliveries").select("card_id,sent_at")
                .eq("status", "sent").gte("sent_at", since).in_("card_id", ids)
                .execute().data or [])
        for r in rows:
            out[r["card_id"]].append(_ts(r["sent_at"]))
    return out


def _notifiable(client, card_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for ids in outbox.chunks(card_ids, 100):
        rows = (client.table("notifiable_cards").select("card_id")
                .in_("card_id", ids).execute().data or [])
        found.update(r["card_id"] for r in rows)
    return found


def _sources(client, notification_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ids in outbox.chunks(notification_ids, 100):
        rows = (client.table("notifications").select("id,source").in_("id", ids)
                .execute().data or [])
        out.update({r["id"]: r["source"] for r in rows})
    return out


def _message(d: dict) -> PassMessage:
    return PassMessage(id=d["id"], header=d["header"], body=d["body"], url=d.get("url"),
                       display_until=_ts(d.get("display_until")))


def deliver(client, d: dict, *, send, now_fn: Callable[[], datetime]) -> str:
    """Push one delivery to the wallet and record the outcome; returns the new status.

    Quota exceeded -> resend silently (`sent_silent`, not billed). Any other error ->
    retry later, `failed` after MAX_ATTEMPTS.
    """
    msg = _message(d)
    try:
        try:
            send(client, d["card_id"], msg, notify=True)
            status = "sent"
        except WalletQuotaExceeded:
            send(client, d["card_id"], msg, notify=False)
            status = "sent_silent"
    except (WalletObjectMissing, LookupError) as exc:
        # No pass to write to (or the card is gone): retrying cannot help.
        _update(client, d["id"], status="skipped", error=f"no_wallet_object: {exc}"[:300],
                claimed_at=None)
        return "skipped"
    except Exception as exc:  # noqa: BLE001 - classified by retry_outcome
        now = now_fn()
        status, not_before = retry_outcome(d.get("attempts", 1), now)
        log.warning("delivery %s failed (attempt %s): %s", d["id"], d.get("attempts"), exc)
        fields = {"status": status, "error": str(exc)[:300], "claimed_at": None}
        if not_before is not None:
            fields["not_before"] = _iso(not_before)
        _update(client, d["id"], **fields)
        return status
    _update(client, d["id"], status=status, sent_at=_iso(now_fn()), error=None)
    return status


def _process_card(client, deliveries: list[dict], ctx: dict) -> dict:
    """All claimed deliveries of ONE card, in order, on one worker — so the daily cap sees
    the sends this same run just made."""
    stats: dict[str, int] = defaultdict(int)
    sent = list(ctx["sent"].get(deliveries[0]["card_id"], []))
    for d in sorted(deliveries, key=lambda x: x["not_before"]):
        now = ctx["now_fn"]()
        decision = decide(
            now=now, source=ctx["sources"].get(d["notification_id"], MANUAL),
            notifiable=d["card_id"] in ctx["notifiable"], rules=ctx["rules"],
            sent_times=sent, display_until=_ts(d.get("display_until")))
        if decision.action == SKIP:
            _update(client, d["id"], status="skipped", error=decision.reason, claimed_at=None)
            stats["skipped"] += 1
        elif decision.action == DEFER:
            _update(client, d["id"], status="pending", not_before=_iso(decision.not_before),
                    claimed_at=None, error=decision.reason,
                    attempts=max(0, d.get("attempts", 1) - 1))   # a deferral is not a failure
            stats["deferred"] += 1
        else:
            status = deliver(client, d, send=ctx["send"], now_fn=ctx["now_fn"])
            stats[status] += 1
            if status == "sent":
                sent.append(now)
    return stats


def process_batch(client, batch: list[dict], *, rules: PlatformRules, send,
                  now_fn: Callable[[], datetime]) -> dict:
    """Decide + send a claimed batch, grouped per card over a small thread pool."""
    card_ids = list({d["card_id"] for d in batch})
    ctx = {"now_fn": now_fn, "rules": rules, "send": send,
           "notifiable": _notifiable(client, card_ids),
           "sent": _sent_times(client, card_ids, now_fn()),
           "sources": _sources(client, list({d["notification_id"] for d in batch}))}
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in batch:
        groups[d["card_id"]].append(d)
    totals: dict[str, int] = defaultdict(int)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for stats in pool.map(lambda g: _process_card(client, g, ctx), groups.values()):
            for k, v in stats.items():
                totals[k] += v
    return totals


def close_finished(client, now: datetime) -> int:
    """Mark `queued` notifications whose deliveries are all settled as `sent`.

    Skips ones younger than CLOSE_GRACE: a just-created notification has no deliveries
    for a moment while its audience is being expanded.
    """
    cutoff = (now - CLOSE_GRACE).isoformat()
    queued = outbox.fetch_all(lambda: client.table("notifications").select("id")
                              .eq("status", "queued").lte("created_at", cutoff).order("id"))
    ids = [r["id"] for r in queued]
    open_ids: set[str] = set()
    for chunk in outbox.chunks(ids, 100):
        rows = (client.table("notification_deliveries").select("notification_id")
                .in_("notification_id", chunk).in_("status", ["pending", "sending"])
                .execute().data or [])
        open_ids.update(r["notification_id"] for r in rows)
    done = [i for i in ids if i not in open_ids]
    for chunk in outbox.chunks(done, 100):
        client.table("notifications").update({"status": "sent", "sent_at": now.isoformat()}).in_(
            "id", chunk).execute()
    return len(done)


def run_dispatch(client, *, send=None, now_fn: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 budget_s: float = RUN_BUDGET_S) -> dict:
    """One dispatcher run: expand due scheduled sends, drain due deliveries in batches of
    200 until the time box ends, then close finished notifications."""
    if send is None:
        from ..wallet_sync import send_card_message
        send = send_card_message
    now_fn = now_fn or (lambda: datetime.now(UTC))
    started = monotonic()
    totals: dict[str, int] = defaultdict(int)
    totals["scheduled_expanded"] = expand_due_scheduled(client, now_fn())
    rules = load_platform_rules(client)
    while monotonic() - started < budget_s:
        batch = client.rpc("claim_notification_deliveries", {"p_limit": BATCH}).execute().data
        if not batch:
            break
        totals["claimed"] += len(batch)
        for k, v in process_batch(client, batch, rules=rules, send=send,
                                  now_fn=now_fn).items():
            totals[k] += v
        if len(batch) < BATCH:
            break
    totals["closed"] = close_finished(client, now_fn())
    return dict(totals)
