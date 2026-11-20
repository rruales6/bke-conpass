"""notifier — scheduled Lambda (no HTTP): the notification dispatcher + the daily automations.

EventBridge invokes it with a `job`:

- `dispatch` (every minute): purge demo campaigns older than 30 minutes, poll Google for
  cards saved to a wallet (never-saved detection), then drain the outbox. The first two are
  best-effort: a failure never stops the dispatch.
- `daily` (08:00 Ecuador = 13:00 UTC): deactivate week-old demo cards, pause cards unused for
  90 days, apply scheduled downgrades, queue "Te extrañamos" / the Pro sequence, then dispatch
  straight away so the 08:00 messages are not left waiting for the next minute tick. Every
  step is isolated: one failing never stops the rest.
- `birthday` (09:00 Ecuador = 14:00 UTC): issue the birthday rewards and queue their
  notices, then dispatch.

Rules live in conpass_common.notify.
"""
from __future__ import annotations

from datetime import UTC, datetime

from conpass_common import billing, card_lifecycle, demo, wallet_saved
from conpass_common.campaigns import ECUADOR_TZ
from conpass_common.db import service_client
from conpass_common.logging import get_logger
from conpass_common.notify import automations, dispatch

log = get_logger(__name__)


def _step(name: str, fn) -> object:
    """Run one step; a failure is logged and reported, never raised."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
        log.warning("notifier step %s failed", name, exc_info=True)
        return {"error": str(exc)}


def run_job(job: str, client) -> dict:
    if job == "dispatch":
        purge = _step("demo_purge", lambda: demo.purge_demo_campaigns(client))
        saved = _step("wallet_saved", lambda: wallet_saved.poll_unsaved(client))
        return {"demo_purge": purge, "wallet_saved": saved,
                "dispatch": dispatch.run_dispatch(client)}
    if job == "daily":
        today = datetime.now(UTC).astimezone(ECUADOR_TZ).date()
        result = {
            "demo_cards": _step("demo_cards", lambda: demo.expire_demo_cards(client)),
            "paused": _step("pause_inactive_cards",
                            lambda: card_lifecycle.pause_inactive_cards(client)),
            "plan_changes": _step("plan_changes",
                                  lambda: billing.apply_scheduled_plan_changes(client, today)),
            "daily": _step("daily", lambda: automations.run_daily(client)),
        }
        result["dispatch"] = _step("dispatch", lambda: dispatch.run_dispatch(client))
        return result
    if job == "birthday":
        return {"birthdays": _step("birthdays", lambda: automations.run_birthdays(client)),
                "dispatch": _step("dispatch", lambda: dispatch.run_dispatch(client))}
    raise ValueError(f"unknown notifier job {job!r}")


def handler(event, context):
    job = (event or {}).get("job", "dispatch")
    result = run_job(job, service_client())
    log.info("notifier %s done: %s", job, result)
    return result
