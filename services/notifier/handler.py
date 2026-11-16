"""notifier — scheduled Lambda (no HTTP): the notification dispatcher + daily automations.

EventBridge invokes it with `{"job": "dispatch"}` every minute (it first purges demo
campaigns older than 30 minutes, best-effort: a purge failure never stops the dispatch) and
`{"job": "daily"}` at 08:00 Ecuador (13:00 UTC). `daily` first deactivates week-old demo cards, then
expires idle balances, queues expiry warnings and inactivity reminders, then dispatches
straight away so the 08:00 messages are not left waiting for the next minute tick.
Rules live in conpass_common.notify.
"""
from __future__ import annotations

from conpass_common import demo
from conpass_common.db import service_client
from conpass_common.logging import get_logger
from conpass_common.notify import automations, dispatch

log = get_logger(__name__)


def run_job(job: str, client) -> dict:
    if job == "dispatch":
        try:
            purge = demo.purge_demo_campaigns(client)
        except Exception as exc:  # noqa: BLE001 - the purge must never block the dispatch
            log.warning("demo campaign purge failed", exc_info=True)
            purge = {"error": str(exc)}
        return {"demo_purge": purge, "dispatch": dispatch.run_dispatch(client)}
    if job == "daily":
        cards = demo.expire_demo_cards(client)
        daily = automations.run_daily(client)
        return {"demo_cards": cards, "daily": daily, "dispatch": dispatch.run_dispatch(client)}
    raise ValueError(f"unknown notifier job {job!r}")


def handler(event, context):
    job = (event or {}).get("job", "dispatch")
    result = run_job(job, service_client())
    log.info("notifier %s done: %s", job, result)
    return result
