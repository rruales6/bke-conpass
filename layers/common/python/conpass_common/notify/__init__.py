"""Wallet notifications (Phase 16) — settings, automations, outbox, dispatch.

Module map (design: docs/design/NOTIFICATIONS.md):
    settings.py     merchant + platform settings loaders (shared with wallet_sync)
    templates.py    automation message texts, es/en, usted register
    automations.py  event automations fired after an accrual (balance/ready/near/milestone)
                    and the daily time-based ones (expiry warning, inactivity, expiring)
    outbox.py       enqueue helpers over notification_deliveries / notifications
    dispatch.py     claims due deliveries, applies quiet hours + daily cap, sends
"""
