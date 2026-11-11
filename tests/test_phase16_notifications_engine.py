"""Phase 16 — notify engine: templates, automation choice, outbox, dispatcher decisions,
daily job. Pure decisions are tested with an injected clock; I/O against tests/fakedb."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from conpass_common.notify import automations as au
from conpass_common.notify import dispatch as dp
from conpass_common.notify import outbox, templates
from conpass_common.notify.settings import MerchantNotificationSettings, PlatformRules
from conpass_common.providers.wallet import WalletQuotaExceeded

from tests.fakedb import FakeDB

M1 = "22222222-2222-2222-2222-222222222222"
C1 = "44444444-4444-4444-4444-444444444444"
C2 = "44444444-4444-4444-4444-444444444445"
P1 = "33333333-3333-3333-3333-333333333333"
S = MerchantNotificationSettings()
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)          # 10:00 Ecuador
DAY = date(2026, 11, 11)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
def F(**kw):
    return templates.MessageFacts(**{"merchant": "Café", "reward": "Un café gratis", **kw})


def test_balance_es_stamps_plural_and_singular():
    two = templates.render("balance", "es", F(n=2, held=3, target=8, missing=5))
    assert two == "Sumó 2 sellos. Lleva 3 / 8: le faltan 5 para su recompensa: Un café gratis."
    one = templates.render("balance", "es", F(n=1, held=7, target=8, missing=1))
    assert one.startswith("Sumó 1 sello. Lleva 7 / 8: le falta 1 ")


def test_balance_points_and_english():
    pts = templates.render("balance", "es", F(mechanic="points", n=1, held=40, target=100,
                                              missing=60))
    assert pts.startswith("Sumó 1 punto. ")
    en = templates.render("balance", "en", F(n=1, held=3, target=8, missing=5))
    assert en == "You earned 1 stamp. You have 3 / 8: 5 to go for Un café gratis."


def test_ready_near_milestone_expiry_inactive():
    assert templates.render("ready", "es", F()).startswith("¡Completó su tarjeta!")
    assert templates.render("ready", "en", F()).startswith("Card complete!")
    assert templates.render("near", "es", F(missing=1)) == (
        "Le falta 1 sello para su recompensa: Un café gratis. ¿Pasa hoy?")
    assert templates.render("near", "es", F(missing=2)).startswith("Le faltan 2 sellos")
    assert templates.render("near", "en", F(missing=1, mechanic="points")) == (
        "Only 1 point to go for Un café gratis. Drop by today?")
    assert templates.render("milestone", "es", F(n=10)) == (
        "¡Su visita n.º 10! Gracias por volver a Café.")
    assert templates.render("milestone", "en", F(n=10)).startswith("Visit number 10!")
    assert templates.render("expiry", "es", F(held=5, days=7)) == (
        "Sus 5 sellos vencen en 7 días. Úselos antes de perderlos.")
    assert templates.render("expiry", "es", F(held=1, days=1)) == (
        "Su 1 sello vence en 1 día. Úselo antes de perderlo.")
    assert templates.render("expiry", "en", F(held=1, days=1)) == (
        "Your 1 stamp expires in 1 day. Use it before you lose it.")
    assert templates.render("inactive", "es", F(held=3, target=8, days=28)).startswith(
        "Hace 28 días que no le vemos.")
    assert templates.render("inactive", "en", F(held=3, target=8, days=1)).startswith(
        "It's been 1 day.")


def test_reward_fallback_and_language_fallback():
    assert "su recompensa: su recompensa." in templates.render(
        "balance", "es", F(reward="  ", n=1, held=1, target=8, missing=7))
    assert "your reward" in templates.render("ready", "en", F(reward=None))
    assert templates.render("ready", "fr", F()).startswith("¡Completó")   # unknown -> es
    with pytest.raises(ValueError):
        templates.render("nope", "es", F())


# --------------------------------------------------------------------------- #
# Event automation choice
# --------------------------------------------------------------------------- #
def ev(**kw):
    base = {"card_id": C1, "merchant_id": M1, "mechanic": "stamps", "before_held": 3,
            "after_held": 4, "gained": 1, "target": 8, "rewards_earned": 0,
            "accrual_count": 3}
    return au.AccrualEvent(**{**base, **kw})


def test_choose_balance_by_default():
    assert au.choose_event(ev(), S) == "balance"


def test_priority_ready_over_milestone_over_near_over_balance():
    # completes a reward on the 10th visit, also within near range -> ready wins
    assert au.choose_event(ev(rewards_earned=1, accrual_count=10, before_held=7,
                              after_held=0), S) == "ready"
    # 10th visit, no reward, crossing near (missing 8-6=2 > 1? use before 5 -> after 7)
    assert au.choose_event(ev(accrual_count=10, before_held=5, after_held=7), S) == "milestone"
    assert au.choose_event(ev(before_held=5, after_held=7), S) == "near"
    assert au.choose_event(ev(before_held=1, after_held=2), S) == "balance"


def test_disabled_automations_fall_through():
    off = MerchantNotificationSettings(ready_on=False)
    assert au.choose_event(ev(rewards_earned=1, after_held=0), off) == "balance"
    nothing = MerchantNotificationSettings(balance_on=False, near_on=False,
                                           milestone_on=False, ready_on=False)
    assert au.choose_event(ev(), nothing) is None


def test_milestone_every_n_accruals():
    assert au.choose_event(ev(accrual_count=10), S) == "milestone"
    assert au.choose_event(ev(accrual_count=20), S) == "milestone"
    assert au.choose_event(ev(accrual_count=11), S) == "balance"


def test_near_fires_only_when_crossing_the_threshold():
    s2 = MerchantNotificationSettings(near_stamps=2)
    # 8 target: missing 4 -> 2 crosses (4 > 2 >= 2)
    assert au.choose_event(ev(before_held=4, after_held=6), s2) == "near"
    # already inside (missing 2 -> 1): not a crossing, plain balance
    assert au.choose_event(ev(before_held=6, after_held=7), s2) == "balance"
    # still outside
    assert au.choose_event(ev(before_held=1, after_held=2), s2) == "balance"
    # completing a reward is never "near"
    assert not au.crossed_near(ev(rewards_earned=1, before_held=7, after_held=1), 2)


def test_near_points_uses_points_threshold():
    pts = ev(mechanic="points", target=100, before_held=60, after_held=92, gained=32)
    assert au.choose_event(pts, MerchantNotificationSettings(near_points=10)) == "near"
    assert au.choose_event(pts, MerchantNotificationSettings(near_points=5)) == "balance"


def test_no_target_means_no_balance_message():
    assert au.choose_event(ev(target=None), S) is None


# --------------------------------------------------------------------------- #
# Outbox
# --------------------------------------------------------------------------- #
def _delivery(db, card, type_, status="pending", notification=None):
    n = notification or next((r for r in db.rows("notifications") if r["type"] == type_),
                             None) or db.insert("notifications", {
        "merchant_id": M1, "source": "automation", "type": type_, "audience": "automatic",
        "day": "2026-11-10", "body": "x"})
    return db.insert("notification_deliveries", {
        "notification_id": n["id"], "merchant_id": M1, "card_id": card, "header": "h",
        "body": "b", "status": status})


def test_enqueue_event_one_batch_per_type_and_day():
    db = FakeDB()
    for _ in range(2):
        outbox.enqueue_event(db, merchant_id=M1, card_id=C1, type_="ready", header="Café",
                             body="¡Completó!", day=DAY)
    outbox.enqueue_event(db, merchant_id=M1, card_id=C2, type_="ready", header="Café",
                         body="¡Completó!", day=DAY)
    assert len(db.rows("notifications")) == 1
    assert len(db.rows("notification_deliveries")) == 3
    outbox.enqueue_event(db, merchant_id=M1, card_id=C1, type_="ready", header="Café",
                         body="x", day=DAY + timedelta(days=1))
    assert len(db.rows("notifications")) == 2


def test_enqueue_reopens_a_closed_batch():
    db = FakeDB()
    outbox.enqueue_event(db, merchant_id=M1, card_id=C1, type_="balance", header="h",
                         body="b", day=DAY)
    db.rows("notifications")[0]["status"] = "sent"
    outbox.enqueue_event(db, merchant_id=M1, card_id=C2, type_="balance", header="h",
                         body="b", day=DAY)
    assert db.rows("notifications")[0]["status"] == "queued"


def test_new_event_supersedes_pending_balance_and_near_only():
    db = FakeDB()
    old_balance = _delivery(db, C1, "balance")
    old_near = _delivery(db, C1, "near")
    expiry = _delivery(db, C1, "expiry")
    other_card = _delivery(db, C2, "balance")
    sent = _delivery(db, C1, "balance", status="sent")
    new_id = outbox.enqueue_event(db, merchant_id=M1, card_id=C1, type_="near", header="h",
                                  body="b", day=DAY)
    status = {r["id"]: r["status"] for r in db.rows("notification_deliveries")}
    assert status[old_balance["id"]] == "superseded"
    assert status[old_near["id"]] == "superseded"
    assert status[expiry["id"]] == "pending"
    assert status[other_card["id"]] == "pending"
    assert status[sent["id"]] == "sent"
    assert status[new_id] == "pending"         # the new one never supersedes itself


def test_timed_enqueue_dedupes_on_key():
    db = FakeDB()
    rows = [{"card_id": C1, "body": "b", "dedupe_key": f"expiry:{C1}:2026-11-01"},
            {"card_id": C2, "body": "b", "dedupe_key": f"expiry:{C2}:2026-11-01"}]
    assert outbox.enqueue_timed(db, merchant_id=M1, type_="expiry", day=DAY, header="h",
                                rows=rows) == 2
    assert outbox.enqueue_timed(db, merchant_id=M1, type_="expiry", day=DAY, header="h",
                                rows=rows) == 0
    assert len(db.rows("notification_deliveries")) == 2


def test_after_accrual_enqueues_localized_message(monkeypatch):
    db = FakeDB(
        cards=[{"id": C1, "language": "en", "program_id": P1}],
        programs=[{"id": P1, "reward": "Free coffee"}],
        merchants=[{"id": M1, "business_name": "Café Vecino"}],
        transactions=[{"id": str(i), "card_id": C1, "kind": "accrue_stamps"}
                      for i in range(10)])
    kind = au.after_accrual(db, ev(accrual_count=0), today=DAY)
    assert kind == "milestone"          # 10 accruals counted from the DB
    d = db.rows("notification_deliveries")[0]
    assert d["header"] == "Café Vecino"
    assert d["body"] == "Visit number 10! Thank you for coming back to Café Vecino."


def test_after_accrual_supersedes_stale_balance():
    db = FakeDB(cards=[{"id": C1, "language": "es", "program_id": P1}],
                programs=[{"id": P1, "reward": "Café"}],
                merchants=[{"id": M1, "business_name": "Café Vecino"}])
    stale = _delivery(db, C1, "balance")
    assert au.after_accrual(db, ev(accrual_count=3), today=DAY) == "balance"
    assert {r["id"]: r["status"] for r in db.rows("notification_deliveries")}[
        stale["id"]] == "superseded"


# --------------------------------------------------------------------------- #
# Dispatcher — pure decisions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utc_h,quiet", [(12, True), (13, False), (1, False), (2, True),
                                         (11, True), (10, True), (0, False)])
def test_in_quiet_hours(utc_h, quiet):
    assert dp.in_quiet_hours(utc(2026, 11, 11, utc_h)) is quiet


def test_quiet_hours_edges_in_ecuador_time():
    assert not dp.in_quiet_hours(utc(2026, 11, 12, 1, 59))     # 20:59 EC
    assert dp.in_quiet_hours(utc(2026, 11, 12, 2, 0))          # 21:00 EC
    assert dp.in_quiet_hours(utc(2026, 11, 11, 12, 59))        # 07:59 EC
    assert not dp.in_quiet_hours(utc(2026, 11, 11, 13, 0))     # 08:00 EC


def test_next_quiet_end_is_next_8am_ecuador():
    assert dp.next_quiet_end(utc(2026, 11, 12, 2, 30)) == utc(2026, 11, 12, 13)   # 21:30 EC
    assert dp.next_quiet_end(utc(2026, 11, 11, 6, 0)) == utc(2026, 11, 11, 13)    # 01:00 EC
    assert dp.next_quiet_end(utc(2026, 11, 11, 13, 0)) == utc(2026, 11, 12, 13)   # exactly 8


RULES = PlatformRules(cap_per_day=2, quiet_hours_enabled=True)


def decide(**kw):
    base = {"now": NOW, "source": "manual", "notifiable": True, "rules": RULES,
            "sent_times": []}
    return dp.decide(**{**base, **kw})


def test_decide_send_by_default():
    assert decide().action == dp.SEND


def test_decide_skips_non_notifiable_and_expired():
    d = decide(notifiable=False)
    assert (d.action, d.reason) == (dp.SKIP, "not_notifiable")
    assert decide(display_until=NOW - timedelta(minutes=1)).action == dp.SKIP


def test_decide_quiet_hours_defers_to_next_8am():
    night = utc(2026, 11, 12, 3)         # 22:00 EC
    d = decide(now=night)
    assert (d.action, d.not_before) == (dp.DEFER, utc(2026, 11, 12, 13))
    assert decide(now=night, rules=PlatformRules(2, False)).action == dp.SEND


def test_decide_cap_manual_deferred_automation_skipped():
    sent = [NOW - timedelta(hours=3), NOW - timedelta(hours=1)]
    m = decide(sent_times=sent)
    assert (m.action, m.not_before) == (dp.DEFER, NOW - timedelta(hours=3) + timedelta(hours=24))
    a = decide(source="automation", sent_times=sent)
    assert (a.action, a.reason) == (dp.SKIP, "daily_cap")
    # one below the cap -> send; sends older than 24 h do not count
    assert decide(sent_times=sent[1:]).action == dp.SEND
    assert decide(sent_times=[NOW - timedelta(hours=25), NOW - timedelta(hours=1)]).action \
        == dp.SEND


def test_cap_release_uses_the_send_that_frees_a_slot():
    t = [NOW - timedelta(hours=h) for h in (5, 3, 1)]
    # cap 2 with 3 recent sends: must wait for the 2nd-oldest (3 h ago) to age out
    assert dp.cap_release_time(t, 2) == NOW - timedelta(hours=3) + timedelta(hours=24)


def test_retry_outcome():
    assert dp.retry_outcome(1, NOW) == ("pending", NOW + timedelta(minutes=5))
    assert dp.retry_outcome(2, NOW) == ("pending", NOW + timedelta(minutes=10))
    assert dp.retry_outcome(3, NOW) == ("failed", None)


# --------------------------------------------------------------------------- #
# Dispatcher — run (I/O)
# --------------------------------------------------------------------------- #
class Wallet:
    """Fake send_card_message: scripted per-card behaviour."""

    def __init__(self, quota=(), fail=()):
        self.calls, self.quota, self.fail = [], set(quota), set(fail)

    def __call__(self, client, card_id, message, *, notify=True):
        self.calls.append((card_id, message.id, notify))
        if card_id in self.fail:
            raise RuntimeError("boom")
        if card_id in self.quota and notify:
            raise WalletQuotaExceeded()


def _dispatch_db(*deliveries, source="manual", notifiable=(C1, C2), sent=(), now=NOW):
    n = {"id": "n1", "merchant_id": M1, "source": source, "type": "general",
         "audience": "all", "status": "queued", "created_at": "2026-11-11T10:00:00+00:00",
         "body": "b", "recipients": len(deliveries)}
    db = FakeDB(notifications=[n], platform_notification_rules=[],
                notifiable_cards=[{"card_id": c} for c in notifiable],
                notification_deliveries=[])
    for i, (card, extra) in enumerate(deliveries):
        db.rows("notification_deliveries").append({
            "id": f"d{i}", "notification_id": "n1", "merchant_id": M1, "card_id": card,
            "header": "h", "body": "b", "url": None, "display_until": None,
            "status": "pending", "not_before": "2026-11-11T14:00:00+00:00", "attempts": 0,
            **extra})
    for j, (card, hours_ago) in enumerate(sent):
        db.rows("notification_deliveries").append({
            "id": f"s{j}", "notification_id": "n0", "merchant_id": M1, "card_id": card,
            "header": "h", "body": "b", "status": "sent", "attempts": 1,
            "sent_at": (now - timedelta(hours=hours_ago)).isoformat(),
            "not_before": "2026-11-10T00:00:00+00:00"})

    def claim(params):
        out = []
        for r in db.rows("notification_deliveries"):
            if r["status"] == "pending" and r["not_before"] <= now_str(NOW) \
                    and len(out) < params["p_limit"]:
                r["status"], r["attempts"] = "sending", r["attempts"] + 1
                out.append(dict(r))
        return out

    db.rpcs["claim_notification_deliveries"] = claim
    return db


def now_str(dt):
    return dt.isoformat()


def run(db, wallet, now=NOW, **kw):
    return dp.run_dispatch(db, send=wallet, now_fn=lambda: now, **kw)


def status_of(db, i):
    return next(r for r in db.rows("notification_deliveries") if r["id"] == f"d{i}")


def test_run_sends_and_closes_notification():
    db = _dispatch_db((C1, {}), (C2, {}))
    w = Wallet()
    stats = run(db, w)
    assert stats["sent"] == 2 and stats["closed"] == 1
    assert {c[2] for c in w.calls} == {True}
    assert status_of(db, 0)["status"] == "sent" and status_of(db, 0)["sent_at"]
    assert db.rows("notifications")[0]["status"] == "sent"


def test_run_quota_resends_silent():
    db = _dispatch_db((C1, {}))
    w = Wallet(quota={C1})
    run(db, w)
    assert status_of(db, 0)["status"] == "sent_silent"
    assert [c[2] for c in w.calls] == [True, False]


def test_run_failure_retries_with_backoff():
    db = _dispatch_db((C1, {}))
    run(db, Wallet(fail={C1}))
    d = status_of(db, 0)
    assert d["status"] == "pending" and d["not_before"] == (NOW + timedelta(minutes=5)).isoformat()
    assert d["attempts"] == 1 and d["error"] == "boom"


def test_run_third_failed_attempt_is_final():
    db = _dispatch_db((C1, {"attempts": 2}))      # claim makes it 3
    run(db, Wallet(fail={C1}))
    assert status_of(db, 0)["status"] == "failed"
    assert db.rows("notifications")[0]["status"] == "sent"   # nothing left pending


def test_run_quiet_hours_defers_without_burning_attempts():
    night = utc(2026, 11, 12, 3)                   # 22:00 EC
    db = _dispatch_db((C1, {}))
    w = Wallet()
    dp.run_dispatch(db, send=w, now_fn=lambda: night)
    d = status_of(db, 0)
    assert w.calls == []
    # claim query uses NOW, so the delivery was claimed then processed at "night"
    assert d["status"] == "pending" and d["not_before"] == utc(2026, 11, 12, 13).isoformat()
    assert d["attempts"] == 0


def test_run_cap_manual_deferred_automation_skipped():
    db = _dispatch_db((C1, {}), sent=[(C1, 3), (C1, 1)])
    run(db, Wallet())
    d = status_of(db, 0)
    assert d["status"] == "pending"
    assert d["not_before"] == (NOW - timedelta(hours=3) + timedelta(hours=24)).isoformat()
    db = _dispatch_db((C1, {}), source="automation", sent=[(C1, 3), (C1, 1)])
    run(db, Wallet())
    assert (status_of(db, 0)["status"], status_of(db, 0)["error"]) == ("skipped", "daily_cap")


def test_run_cap_counts_sends_made_in_the_same_run():
    db = _dispatch_db((C1, {}), (C1, {}), (C1, {}))
    run(db, Wallet())
    st = sorted(status_of(db, i)["status"] for i in range(3))
    assert st == ["pending", "sent", "sent"]       # cap 2 -> third deferred


def test_run_skips_non_notifiable_card():
    db = _dispatch_db((C1, {}), notifiable=())
    w = Wallet()
    run(db, w)
    assert w.calls == [] and status_of(db, 0)["status"] == "skipped"


def test_run_time_box_stops_claiming():
    db = _dispatch_db((C1, {}))
    ticks = iter([0.0, 1000.0, 2000.0])
    stats = dp.run_dispatch(db, send=Wallet(), now_fn=lambda: NOW,
                            monotonic=lambda: next(ticks))
    assert stats.get("claimed", 0) == 0
    assert status_of(db, 0)["status"] == "pending"


def test_run_does_not_close_young_or_open_notifications():
    db = _dispatch_db((C1, {"not_before": "2026-11-12T00:00:00+00:00"}))   # not due
    run(db, Wallet())
    assert db.rows("notifications")[0]["status"] == "queued"
    db = _dispatch_db()
    db.rows("notifications")[0]["created_at"] = NOW.isoformat()          # just created
    run(db, Wallet())
    assert db.rows("notifications")[0]["status"] == "queued"


def test_run_expands_due_scheduled():
    db = _dispatch_db()
    db.rows("notifications").append({"id": "n2", "status": "scheduled",
                                     "scheduled_at": "2026-11-11T14:00:00+00:00"})
    db.rows("notifications").append({"id": "n3", "status": "scheduled",
                                     "scheduled_at": "2026-12-01T14:00:00+00:00"})
    db.rpcs["enqueue_manual_notification"] = lambda p: []
    stats = run(db, Wallet())
    assert stats["scheduled_expanded"] == 1
    assert ("enqueue_manual_notification", {"p_notification": "n2"}) in db.rpc_calls


def test_run_processes_in_batches_of_200():
    db = _dispatch_db(*[(C1, {})] * 1, (C2, {}))
    seen = []
    orig = db.rpcs["claim_notification_deliveries"]
    db.rpcs["claim_notification_deliveries"] = lambda p: (seen.append(p["p_limit"]) or orig(p))
    run(db, Wallet())
    assert seen[0] == 200


# --------------------------------------------------------------------------- #
# Daily job — pure selection
# --------------------------------------------------------------------------- #
def card(cid=C1, stamps=3, days_ago=0, **kw):
    base = {"card_id": cid, "program_type": "loyalty_stamps", "stamps": stamps, "points": 0,
            "target": 8, "last_at": NOW - timedelta(days=days_ago, hours=1)}
    return au.CardInfo(**{**base, **kw})


def test_window_predicates():
    n, w = 30, 7
    assert au.is_expired(NOW - timedelta(days=30), NOW, n)
    assert not au.is_expired(NOW - timedelta(days=29), NOW, n)
    assert au.in_expiry_warning_window(NOW - timedelta(days=23), NOW, n, w)       # edge in
    assert not au.in_expiry_warning_window(NOW - timedelta(days=22), NOW, n, w)
    assert not au.in_expiry_warning_window(NOW - timedelta(days=30), NOW, n, w)   # expired
    assert au.in_inactive_window(NOW - timedelta(days=28), NOW, 28)
    assert au.in_inactive_window(NOW - timedelta(days=34), NOW, 28)
    assert not au.in_inactive_window(NOW - timedelta(days=35), NOW, 28)
    assert not au.in_inactive_window(NOW - timedelta(days=27), NOW, 28)


def test_days_until_expiry_rounds_up_min_one():
    assert au.days_until_expiry(NOW - timedelta(days=23, hours=1), NOW, 30) == 7
    assert au.days_until_expiry(NOW - timedelta(days=29, hours=23), NOW, 30) == 1


def test_plan_daily_selection():
    s = MerchantNotificationSettings(balance_expiry_days=30, expiry_warn_days=7,
                                     inactive_on=True, inactive_days=14)
    cards = [card(C1, days_ago=31),                    # expires
             card(C2, days_ago=25),                    # warned (5 days left)
             card("c3", days_ago=15),                  # inactive reminder
             card("c4", days_ago=2),                   # nothing
             card("c5", stamps=0, days_ago=31),        # no balance: untouched
             card("c6", days_ago=31, program_type="loyalty_points", stamps=0, points=50)]
    plan = au.plan_daily(cards, s, NOW, inactive_allowed=True)
    assert [c.card_id for c in plan.expire] == [C1, "c6"]
    assert [(c.card_id, d) for c, d in plan.warn] == [(C2, 5)]
    assert [c.card_id for c, _ in plan.inactive] == ["c3"]
    assert au.plan_daily(cards, s, NOW, inactive_allowed=False).inactive == []


def test_plan_daily_inert_without_window_and_respects_switches():
    cards = [card(days_ago=400)]
    p = au.plan_daily(cards, MerchantNotificationSettings(), NOW, inactive_allowed=True)
    assert (p.expire, p.warn, p.inactive) == ([], [], [])
    s = MerchantNotificationSettings(balance_expiry_days=30, expiry_on=False)
    assert au.plan_daily([card(days_ago=25)], s, NOW, inactive_allowed=True).warn == []


def test_dedupe_key_shape():
    assert au.dedupe_key("expiry", C1, utc(2026, 10, 1, 3)) == f"expiry:{C1}:2026-10-01"


# --------------------------------------------------------------------------- #
# Daily job — run (I/O)
# --------------------------------------------------------------------------- #
def _daily_db(tier="pro", **settings):
    st = {"merchant_id": M1, **MerchantNotificationSettings(**settings).as_row()}
    old, warn, idle = (NOW - timedelta(days=d, hours=1) for d in (31, 25, 15))
    db = FakeDB(
        merchant_notification_settings=[st],
        subscriptions=[{"merchant_id": M1, "tier": tier}],
        merchants=[{"id": M1, "business_name": "Café Vecino"}],
        programs=[{"id": P1, "reward": "Un café"}],
        notifiable_cards=[
            {"card_id": cid, "merchant_id": M1, "program_id": P1,
             "program_type": "loyalty_stamps", "stamps": 3, "points": 0,
             "stamps_for_reward": 8, "points_for_reward": None}
            for cid in (C1, C2, "c3")],
        card_last_activity=[
            {"card_id": C1, "merchant_id": M1, "last_at": old.isoformat()},
            {"card_id": C2, "merchant_id": M1, "last_at": warn.isoformat()},
            {"card_id": "c3", "merchant_id": M1, "last_at": idle.isoformat()}],
        cards=[{"id": cid, "merchant_id": M1, "language": lang, "stamps": 3, "points": 0}
               for cid, lang in ((C1, "es"), (C2, "en"), ("c3", "es"))])
    return db


def test_run_daily_expires_warns_and_dedupes():
    db = _daily_db(balance_expiry_days=30, inactive_on=True, inactive_days=14)
    pushed = []
    totals = au.run_daily(db, NOW, push=lambda c, cid: pushed.append(cid))
    assert totals["expired"] == 1 and totals["warned"] == 1 and totals["inactive"] == 1
    card1 = next(r for r in db.rows("cards") if r["id"] == C1)
    assert (card1["stamps"], card1["points"]) == (0, 0)
    audit = db.rows("balance_expirations")[0]
    assert (audit["card_id"], audit["stamps_expired"], audit["merchant_id"]) == (C1, 3, M1)
    assert pushed == [C1]
    bodies = {d["card_id"]: d for d in db.rows("notification_deliveries")}
    assert bodies[C2]["body"].startswith("Your 3 stamps expire in 5 days")
    assert bodies[C2]["dedupe_key"].startswith(f"expiry:{C2}:")
    assert bodies["c3"]["dedupe_key"].startswith("inactive:c3:")
    # second run (same day): nothing to expire (now 0), nothing new inserted
    again = au.run_daily(db, NOW, push=lambda c, cid: None)
    assert again["expired"] == 0 and again["warned"] == 0 and again["inactive"] == 0
    assert len(db.rows("notification_deliveries")) == 2


def test_run_daily_inactive_needs_pro_and_push_failure_is_swallowed():
    db = _daily_db(tier="growth", balance_expiry_days=30, inactive_on=True, inactive_days=14)

    def boom(c, cid):
        raise RuntimeError("wallet")
    totals = au.run_daily(db, NOW, push=boom)
    assert totals["expired"] == 1 and totals["inactive"] == 0 and totals["errors"] == 0


def test_run_daily_skips_merchants_with_nothing_enabled():
    db = _daily_db()          # defaults: no expiry window, inactive off
    assert au.run_daily(db, NOW, push=lambda *a: None)["merchants"] == 0
    assert db.rows("notification_deliveries") == []
