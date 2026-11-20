"""Phase 18 (B2) — notification settings v3.0, PATCH, sequence / level-up / birthday
automations, the 48 h display window and the notifier jobs. Fakes only: no live services."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from conpass_common.notify import automations as au
from conpass_common.notify import outbox, templates
from conpass_common.notify.settings import MerchantNotificationSettings, load_merchant_settings

from tests.fakedb import FakeDB
from tests.test_phase16_notifications_engine import NOW, Wallet, _dispatch_db, run
from tests.test_phase16_notifications_service import (
    GENERAL,
    L1,
    M1,
    M2,
    _auto,
    _future,
    _svc,
)

C1 = "44444444-4444-4444-4444-444444444444"
C2 = "44444444-4444-4444-4444-444444444445"
C3 = "44444444-4444-4444-4444-444444444446"
P1 = "33333333-3333-3333-3333-333333333333"
P2 = "33333333-3333-3333-3333-333333333334"
CU1 = "77777777-7777-7777-7777-777777777771"
CU2 = "77777777-7777-7777-7777-777777777772"


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def _settings_row(**over):
    return {"merchant_id": M1, **MerchantNotificationSettings(**over).as_row()}


def _steps(*days, text="Vuelve a {negocio}"):
    return [{"day": d, "text": text} for d in days]


# --------------------------------------------------------------------------- #
# Settings: validation and plan gates
# --------------------------------------------------------------------------- #
def _put(c, **automations):
    body = _auto()
    body["automations"].update(automations)
    return c.put("/notifications/settings", json=body)


def test_sequence_pro_roundtrip_and_storage(monkeypatch):
    c, db = _svc(monkeypatch, tier="pro")
    r = _put(c, sequence={"enabled": True, "steps": _steps(7, 14, 45)})
    assert r.status_code == 200, r.text
    assert [s["day"] for s in r.json()["automations"]["sequence"]["steps"]] == [7, 14, 45]
    assert r.json()["automations"]["sequence"]["enabled"] is True
    row = db.rows("merchant_notification_settings")[0]
    assert row["sequence_on"] is True and row["sequence_steps"][2] == {
        "day": 45, "text": "Vuelve a {negocio}"}


@pytest.mark.parametrize("steps", [_steps(14, 14), _steps(30, 20)])
def test_sequence_days_must_ascend(monkeypatch, steps):
    c, _ = _svc(monkeypatch, tier="pro")
    r = _put(c, sequence={"enabled": True, "steps": steps})
    assert r.status_code == 422
    assert r.json()["details"]["reason"] == "sequence_days_not_ascending"


def test_sequence_text_required_only_while_enabled(monkeypatch):
    c, _ = _svc(monkeypatch, tier="pro")
    blank = [{"day": 7, "text": "  "}]
    assert _put(c, sequence={"enabled": True, "steps": blank}).status_code == 422
    assert _put(c, sequence={"enabled": False, "steps": blank}).status_code == 200


@pytest.mark.parametrize("steps", [[], _steps(7, 14, 21, 28), _steps(6), _steps(90)])
def test_sequence_shape_limits_422(monkeypatch, steps):
    c, _ = _svc(monkeypatch, tier="pro")
    assert _put(c, sequence={"enabled": True, "steps": steps}).status_code == 422


def test_inactive_days_bounds(monkeypatch):
    c, _ = _svc(monkeypatch)
    assert _put(c, inactive={"enabled": True, "days": 6}).status_code == 422
    assert _put(c, inactive={"enabled": True, "days": 90}).status_code == 422
    assert _put(c, inactive={"enabled": True, "days": 89}).status_code == 200


@pytest.mark.parametrize("patch", [
    {"sequence": {"enabled": True, "steps": _steps(30)}},
    # Plan gate before field validation (live E2E finding): blank texts still answer 402.
    {"sequence": {"enabled": True, "steps": [{"day": 30, "text": ""}]}},
    {"levelUp": {"enabled": True}},
    {"birthday": {"enabled": True, "benefit": "gift", "gift": "Postre", "validityDays": 7}},
])
@pytest.mark.parametrize("tier", ["starter", "growth"])
def test_pro_automations_need_pro_402(monkeypatch, patch, tier):
    c, db = _svc(monkeypatch, tier=tier)
    r = _put(c, **patch)
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    assert r.json()["details"]["requiredTier"] == "pro"
    assert db.rows("merchant_notification_settings") == []


def test_level_up_and_birthday_roundtrip_for_pro(monkeypatch):
    c, db = _svc(monkeypatch, tier="pro")
    r = _put(c, levelUp={"enabled": True},
             birthday={"enabled": True, "benefit": "discount", "discountPercent": 20,
                       "validityDays": 14})
    assert r.status_code == 200, r.text
    a = r.json()["automations"]
    assert a["levelUp"] == {"enabled": True}
    assert a["birthday"] == {"enabled": True, "benefit": "discount", "discountPercent": 20,
                             "validityDays": 14}
    row = db.rows("merchant_notification_settings")[0]
    assert row["level_up_on"] and row["birthday_discount_percent"] == 20


@pytest.mark.parametrize("birthday,reason", [
    ({"enabled": True, "benefit": "gift", "validityDays": 7}, "birthday_gift_required"),
    ({"enabled": True, "benefit": "gift", "gift": "  ", "validityDays": 7},
     "birthday_gift_required"),
    ({"enabled": True, "benefit": "discount", "validityDays": 7}, "birthday_discount_required"),
])
def test_birthday_benefit_required(monkeypatch, birthday, reason):
    c, _ = _svc(monkeypatch, tier="pro")
    r = _put(c, birthday=birthday)
    assert r.status_code == 422 and r.json()["details"]["reason"] == reason


@pytest.mark.parametrize("validity,gift,pct", [(0, "x", None), (31, "x", None),
                                                (7, "x" * 81, None), (7, None, 101)])
def test_birthday_field_ranges(monkeypatch, validity, gift, pct):
    c, _ = _svc(monkeypatch, tier="pro")
    bd = {"enabled": True, "benefit": "gift" if gift else "discount", "validityDays": validity}
    if gift:
        bd["gift"] = gift
    if pct:
        bd["discountPercent"] = pct
    assert _put(c, birthday=bd).status_code == 422


def test_custom_texts_need_pro_and_map_keys(monkeypatch):
    texts = {"ready": "Listo en {negocio}", "levelUp": "Nivel {nivel}", "inactive": "Vuelve"}
    c, db = _svc(monkeypatch, tier="growth")
    r = c.put("/notifications/settings", json={**_auto(), "customTexts": texts})
    assert r.status_code == 402 and r.json()["code"] == "feature_not_in_plan"
    c, db = _svc(monkeypatch, tier="pro")
    r = c.put("/notifications/settings", json={**_auto(), "customTexts": texts})
    assert r.status_code == 200, r.text
    assert r.json()["customTexts"] == texts
    assert db.rows("merchant_notification_settings")[0]["custom_texts"] == {
        "ready": "Listo en {negocio}", "level_up": "Nivel {nivel}", "inactive": "Vuelve"}


def test_empty_custom_texts_are_free_and_downgrade_keeps_stored(monkeypatch):
    c, db = _svc(monkeypatch, tier="starter", merchant_notification_settings=[
        _settings_row(custom_texts={"ready": "Mi texto"})])
    same = {**_auto(), "customTexts": {"ready": "Mi texto", "near": ""}}
    assert c.put("/notifications/settings", json=same).status_code == 200
    changed = {**_auto(), "customTexts": {"ready": "Otro"}}
    assert c.put("/notifications/settings", json=changed).status_code == 402
    assert c.put("/notifications/settings", json=_auto()).status_code == 200
    assert db.rows("merchant_notification_settings")[0]["custom_texts"] == {}


def test_custom_text_over_150_is_422(monkeypatch):
    c, _ = _svc(monkeypatch, tier="pro")
    r = c.put("/notifications/settings", json={**_auto(), "customTexts": {"ready": "x" * 151}})
    assert r.status_code == 422


def test_settings_show_birthdays_on_file_and_stats(monkeypatch):
    c, _ = _svc(
        monkeypatch, tier="pro",
        customers=[{"id": CU1, "merchant_id": M1, "birthday_month": 3, "birthday_day": 4},
                   {"id": CU2, "merchant_id": M1},
                   {"id": "x", "merchant_id": M2, "birthday_month": 3, "birthday_day": 4}])
    body = c.get("/notifications/settings").json()
    assert body["birthdaysOnFile"] == 1
    assert body["automationStats"] == {}


def test_missing_row_defaults_match_the_migration():
    s = load_merchant_settings(FakeDB(), M1)
    assert (s.balance_on, s.inactive_on, s.inactive_days) == (False, True, 30)
    assert (s.sequence_on, s.level_up_on, s.birthday_on) == (False, False, False)
    assert (s.milestone_on, s.expiry_on, s.balance_expiry_days) == (False, False, None)


# --------------------------------------------------------------------------- #
# Automation stats
# --------------------------------------------------------------------------- #
def _stats_db():
    sent = NOW - timedelta(days=2)
    return FakeDB(
        notifications=[
            {"id": "nr", "merchant_id": M1, "source": "automation", "type": "ready",
             "day": "2026-11-09"},
            {"id": "nl", "merchant_id": M1, "source": "automation", "type": "level_up",
             "day": "2026-11-09"},
            {"id": "nb", "merchant_id": M1, "source": "automation", "type": "balance",
             "day": "2026-09-01"},                         # batch too old
            {"id": "nx", "merchant_id": M2, "source": "automation", "type": "ready",
             "day": "2026-11-09"}],                        # another merchant
        notification_deliveries=[
            {"id": "d1", "notification_id": "nr", "card_id": C1, "status": "sent",
             "sent_at": sent.isoformat()},
            {"id": "d2", "notification_id": "nr", "card_id": C2, "status": "sent",
             "sent_at": sent.isoformat()},
            {"id": "d3", "notification_id": "nr", "card_id": C3, "status": "skipped",
             "sent_at": sent.isoformat()},
            {"id": "d4", "notification_id": "nl", "card_id": C1, "status": "sent",
             "sent_at": sent.isoformat()},
            {"id": "d5", "notification_id": "nb", "card_id": C1, "status": "sent",
             "sent_at": (NOW - timedelta(days=60)).isoformat()}],
        # the view already drops reversed operations and reversals
        effective_transactions=[
            {"id": "t1", "card_id": C1, "kind": "accrue_stamps",
             "created_at": (sent + timedelta(hours=47)).isoformat()},     # inside 48 h
            {"id": "t2", "card_id": C2, "kind": "accrue_stamps",
             "created_at": (sent + timedelta(hours=49)).isoformat()},     # too late
            {"id": "t3", "card_id": C2, "kind": "accrue_stamps",
             "created_at": (sent - timedelta(hours=1)).isoformat()},      # before the send
            {"id": "t4", "card_id": C3, "kind": "accrue_stamps",
             "created_at": (sent + timedelta(hours=1)).isoformat()}])     # not delivered


def test_automation_stats_last_30_days_with_visits():
    stats = au.automation_stats(_stats_db(), M1, NOW)
    assert stats == {"ready": {"sent": 2, "visits48h": 1},
                     "levelUp": {"sent": 1, "visits48h": 1}}


def test_automation_stats_ignores_reversed_operations():
    db = _stats_db()
    db.rows("effective_transactions").clear()
    # present only in the raw table: undone, so the view (what we read) excludes it
    db.rows("transactions").append({"id": "t9", "card_id": C1, "kind": "accrue_stamps",
                                    "created_at": (NOW - timedelta(days=2)).isoformat(),
                                    "reversed_at": NOW.isoformat()})
    assert au.automation_stats(db, M1, NOW)["ready"] == {"sent": 2, "visits48h": 0}


def test_settings_endpoint_returns_stats(monkeypatch):
    from services.notifications import handler as h
    c, _ = _svc(monkeypatch, tier="pro")
    monkeypatch.setattr(h, "automation_stats",
                        lambda client, merchant_id: {"ready": {"sent": 4, "visits48h": 1}})
    assert c.get("/notifications/settings").json()["automationStats"] == {
        "ready": {"sent": 4, "visits48h": 1}}
    assert c.put("/notifications/settings", json=_auto()).json()["automationStats"] == {
        "ready": {"sent": 4, "visits48h": 1}}


# --------------------------------------------------------------------------- #
# PATCH /notifications/{id}
# --------------------------------------------------------------------------- #
def _sched(**over):
    return {"id": str(uuid.uuid4()), "merchant_id": M1, "source": "manual", "type": "general",
            "audience": "all", "body": "Hola", "status": "scheduled", "recipients": 0,
            "scheduled_at": _future(3), "created_at": "2026-11-01T00:00:00+00:00", **over}


def test_patch_merges_and_keeps_scheduled(monkeypatch):
    row = _sched(title="Antes")
    c, db = _svc(monkeypatch, tier="growth", notifications=[row])
    new_at = _future(5)
    r = c.patch(f"/notifications/{row['id']}", json={"body": "Nuevo", "scheduledAt": new_at})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "Nuevo" and r.json()["status"] == "scheduled"
    assert r.json()["title"] == "Antes"                      # untouched fields survive
    stored = db.rows("notifications")[0]
    assert stored["body"] == "Nuevo" and stored["status"] == "scheduled"
    assert datetime.fromisoformat(stored["scheduled_at"]) == datetime.fromisoformat(new_at)
    assert [x for x in db.rpc_calls if x[0] == "enqueue_manual_notification"] == []


def test_patch_can_change_type_and_clear_url(monkeypatch):
    row = _sched(url="https://x.co/a", type="flash", flash_hours=4)
    c, db = _svc(monkeypatch, tier="growth", notifications=[row])
    r = c.patch(f"/notifications/{row['id']}", json={"type": "general", "url": None})
    assert r.status_code == 200, r.text
    stored = db.rows("notifications")[0]
    assert stored["type"] == "general" and stored["url"] is None
    assert stored["flash_hours"] is None


@pytest.mark.parametrize("status", ["queued", "sent", "cancelled"])
def test_patch_only_while_scheduled(monkeypatch, status):
    row = _sched(status=status)
    c, db = _svc(monkeypatch, tier="growth", notifications=[row])
    assert c.patch(f"/notifications/{row['id']}", json={"body": "x"}).status_code == 409
    assert db.rows("notifications")[0]["body"] == "Hola"


def test_patch_not_found_and_foreign(monkeypatch):
    foreign = _sched(merchant_id=M2)
    c, _ = _svc(monkeypatch, tier="growth", notifications=[foreign])
    assert c.patch(f"/notifications/{foreign['id']}", json={"body": "x"}).status_code == 404
    assert c.patch(f"/notifications/{uuid.uuid4()}", json={"body": "x"}).status_code == 404


@pytest.mark.parametrize("patch", [
    {"scheduledAt": "2020-01-01T00:00:00Z"},
    {"scheduledAt": "2999-01-01T00:00:00Z"},
    {"audience": "level"},                                  # levelMin missing
    {"type": "event"},                                      # eventAt missing
    {"type": "flash"},                                      # flashHours missing
    {"audience": "location", "locationId": "66666666-6666-6666-6666-666666666667"},
])
def test_patch_same_validation_as_create(monkeypatch, patch):
    row = _sched()
    c, db = _svc(monkeypatch, tier="growth", notifications=[row])
    r = c.patch(f"/notifications/{row['id']}", json=patch)
    assert r.status_code == 422, r.text
    assert db.rows("notifications")[0]["audience"] == "all"


def test_patch_same_plan_gates_as_create(monkeypatch):
    row = _sched()
    c, db = _svc(monkeypatch, tier="starter", notifications=[row])
    r = c.patch(f"/notifications/{row['id']}", json={"audience": "near"})
    assert r.status_code == 402 and r.json()["details"]["requiredTier"] == "growth"
    assert c.patch(f"/notifications/{row['id']}", json={"url": "https://x.co"}).status_code == 402
    assert c.patch(f"/notifications/{row['id']}", json={"body": "ok"}).status_code == 200


def test_patch_segment_to_own_locale(monkeypatch):
    row = _sched()
    c, _ = _svc(monkeypatch, tier="growth", notifications=[row])
    r = c.patch(f"/notifications/{row['id']}", json={"audience": "location", "locationId": L1})
    assert r.status_code == 200 and r.json()["locationName"] == "Centro"


def test_patch_is_not_a_new_send_so_demo_limit_does_not_apply(monkeypatch):
    row = _sched()
    sent_today = [_sched(status="sent", created_at=datetime.now(UTC).isoformat())
                  for _ in range(5)]
    c, db = _svc(monkeypatch, tier="growth", notifications=[row, *sent_today],
                 merchants=[{"id": M1, "is_demo": True}])
    assert c.post("/notifications", json=GENERAL).status_code == 429
    assert c.patch(f"/notifications/{row['id']}", json={"body": "ok"}).status_code == 200


# --------------------------------------------------------------------------- #
# Templates: custom text, placeholders, length
# --------------------------------------------------------------------------- #
def test_custom_text_placeholders():
    facts = templates.MessageFacts(merchant="Café Vecino", reward="Un café", missing=2, level=3)
    out = templates.render("near", "es", facts,
                           custom="Faltan {faltan} para {premio} en {negocio}. Nivel {nivel}")
    assert out == "Faltan 2 para Un café en Café Vecino. Nivel 3"
    assert templates.render("near", "es", facts, custom="   ").startswith("Te faltan 2")


def test_every_body_is_cut_to_150():
    facts = templates.MessageFacts(merchant="C", reward="x " * 100, missing=2, held=1, target=8)
    for kind in ("ready", "near", "balance", "inactive", "level_up", "birthday"):
        assert len(templates.render(kind, "es", facts)) <= 150
    long = templates.render("ready", "es", facts, custom="palabra " * 40)
    assert len(long) <= 150 and long.endswith("…")


def test_new_fixed_templates_in_tu_register():
    f = templates.MessageFacts(merchant="Café", level=3, reward="Un postre de la casa", days=7)
    assert templates.render("level_up", "es", f) == (
        "Subiste a nivel 3. Gracias por volver a Café.")
    assert templates.render("birthday", "es", f) == (
        "Feliz cumpleaños. Te regalamos un postre de la casa, válido 7 días en Café.")
    assert "!" not in templates.render("birthday", "es", f)
    assert templates.render("inactive", "es", templates.MessageFacts(
        merchant="Café", days=30, held=0, target=8)) == (
        "Hace 30 días que no te vemos. Te esperamos en Café.")
    assert templates.render("level_up", "en", f).startswith("You reached level 3.")


# --------------------------------------------------------------------------- #
# Sequence and "Te extrañamos" planning
# --------------------------------------------------------------------------- #
def _card(cid=C1, days_ago=0, **kw):
    base = {"card_id": cid, "program_type": "loyalty_stamps", "stamps": 3, "points": 0,
            "target": 8, "last_at": NOW - timedelta(days=days_ago, hours=1)}
    return au.CardInfo(**{**base, **kw})


def _seq(*days):
    return MerchantNotificationSettings(
        sequence_on=True, sequence_steps=[{"day": d, "text": f"paso {d}"} for d in days])


def test_plan_sequence_one_step_per_window():
    s = _seq(7, 14, 30)
    cards = [_card("a", 8), _card("b", 14), _card("c", 20), _card("d", 31), _card("e", 6),
             _card("f", 38)]
    plan = au.plan_daily(cards, s, NOW, sequence_active=True)
    got = {c.card_id: (step, text) for c, step, text, _ in plan.sequence}
    assert got == {"a": (0, "paso 7"), "b": (1, "paso 14"), "c": (1, "paso 14"),
                   "d": (2, "paso 30")}              # e too early, f past the last window
    assert plan.inactive == []                       # the sequence replaces the single reminder


def test_plan_without_active_sequence_uses_the_single_reminder():
    s = MerchantNotificationSettings(sequence_on=True, inactive_days=30,
                                     sequence_steps=_steps(7))
    plan = au.plan_daily([_card("a", 8), _card("b", 31)], s, NOW, sequence_active=False)
    assert plan.sequence == [] and [c.card_id for c, _ in plan.inactive] == ["b"]


def _seq_db(tier="pro", **over):
    idle = NOW - timedelta(days=8, hours=1)
    settings = {"sequence_on": True, "sequence_steps": [
        {"day": 7, "text": "Te echamos de menos en {negocio}"}, {"day": 14, "text": "Dos semanas"}],
        **over}
    return FakeDB(
        merchant_notification_settings=[_settings_row(**settings)],
        subscriptions=[{"merchant_id": M1, "tier": tier}],
        merchants=[{"id": M1, "business_name": "Café Vecino", "is_demo": False}],
        programs=[{"id": P1, "reward": "Un café", "merchant_id": M1}],
        notifiable_cards=[{"card_id": C1, "merchant_id": M1, "program_id": P1,
                           "program_type": "loyalty_stamps", "stamps": 3, "points": 0,
                           "stamps_for_reward": 8, "points_for_reward": None, "level": 2}],
        card_last_activity=[{"card_id": C1, "merchant_id": M1, "last_at": idle.isoformat()}],
        cards=[{"id": C1, "merchant_id": M1, "language": "es"}])


def test_run_daily_sequence_pro_replaces_inactive_and_is_idempotent():
    db = _seq_db()
    totals = au.run_daily(db, NOW)
    assert totals["sequence"] == 1 and totals["inactive"] == 0
    d = db.rows("notification_deliveries")[0]
    assert d["body"] == "Te echamos de menos en Café Vecino"
    assert d["dedupe_key"] == f"sequence:{C1}:{(NOW - timedelta(days=8, hours=1)).date()}:0"
    assert next(n for n in db.rows("notifications"))["type"] == "sequence"
    assert au.run_daily(db, NOW)["sequence"] == 0
    assert len(db.rows("notification_deliveries")) == 1


def test_run_daily_sequence_stops_when_the_customer_returns():
    db = _seq_db()
    au.run_daily(db, NOW)
    # the customer visits: last use moves, the old absence is over
    db.rows("card_last_activity")[0]["last_at"] = (NOW - timedelta(days=1)).isoformat()
    assert au.run_daily(db, NOW)["sequence"] == 0
    # a later absence reaches step 1 again under a NEW key
    later = NOW + timedelta(days=8)
    assert au.run_daily(db, later)["sequence"] == 1
    keys = [d["dedupe_key"] for d in db.rows("notification_deliveries")]
    assert len(set(keys)) == 2


def test_run_daily_sequence_only_while_still_pro():
    db = _seq_db(tier="growth", inactive_days=7)
    totals = au.run_daily(db, NOW)
    assert totals["sequence"] == 0 and totals["inactive"] == 1       # falls back to the basic one
    d = db.rows("notification_deliveries")[0]
    assert d["dedupe_key"].startswith("inactive:")
    assert d["body"].startswith("Hace 8 días que no te vemos")        # fixed text, not the step's


def test_run_daily_inactive_custom_text_is_pro_only():
    for tier, expected in (("pro", "Vuelve a Café Vecino"),
                           ("growth", "Hace 8 días que no te vemos.")):
        db = _seq_db(tier=tier, sequence_on=False, inactive_days=7,
                     custom_texts={"inactive": "Vuelve a {negocio}"})
        au.run_daily(db, NOW)
        assert db.rows("notification_deliveries")[0]["body"].startswith(expected)


def test_daily_deliveries_carry_a_48h_display_window():
    db = _seq_db()
    au.run_daily(db, NOW)
    d = db.rows("notification_deliveries")[0]
    assert datetime.fromisoformat(d["display_until"]) == NOW + timedelta(hours=48)


# --------------------------------------------------------------------------- #
# Level-up
# --------------------------------------------------------------------------- #
def _level_db(tier="pro", **over):
    return FakeDB(
        merchant_notification_settings=[_settings_row(level_up_on=True, **over)],
        subscriptions=[{"merchant_id": M1, "tier": tier}],
        merchants=[{"id": M1, "business_name": "Café Vecino", "is_demo": False}],
        programs=[{"id": P1, "reward": "Un café"}],
        cards=[{"id": C1, "merchant_id": M1, "program_id": P1, "language": "es"}])


def _redeem(db, **kw):
    return au.after_redemption(db, card_id=C1, merchant_id=M1, program_id=P1, new_level=3,
                               today=date(2026, 11, 11), **kw)


def test_after_redemption_queues_level_up():
    db = _level_db()
    assert _redeem(db) == "level_up"
    d = db.rows("notification_deliveries")[0]
    assert d["body"] == "Subiste a nivel 3. Gracias por volver a Café Vecino."
    assert d["header"] == "Café Vecino" and d["card_id"] == C1
    assert db.rows("notifications")[0]["type"] == "level_up"
    assert d["display_until"] is not None


def test_after_redemption_custom_text_and_gates():
    db = _level_db(custom_texts={"level_up": "Nivel {nivel} en {negocio}"})
    assert _redeem(db) == "level_up"
    assert db.rows("notification_deliveries")[0]["body"] == "Nivel 3 en Café Vecino"
    assert _redeem(_level_db(tier="growth")) is None             # no longer Pro
    off = _level_db()
    off.rows("merchant_notification_settings")[0]["level_up_on"] = False
    assert _redeem(off) is None
    assert off.rows("notification_deliveries") == []
    demo = _level_db()
    demo.rows("merchants")[0]["is_demo"] = True
    assert _redeem(demo) is None
    assert _redeem(FakeDB()) is None                             # no settings row = off


def test_after_redemption_never_raises():
    class Boom:
        def table(self, *_):
            raise RuntimeError("db down")
    assert _redeem(Boom()) is None


def test_after_accrual_uses_custom_text_only_for_pro():
    def make(tier):
        return FakeDB(
            merchant_notification_settings=[_settings_row(
                custom_texts={"ready": "Tu premio en {negocio}: {premio}"})],
            subscriptions=[{"merchant_id": M1, "tier": tier}],
            merchants=[{"id": M1, "business_name": "Café Vecino"}],
            programs=[{"id": P1, "reward": "Un café"}],
            cards=[{"id": C1, "language": "es", "program_id": P1}])
    ev = au.AccrualEvent(card_id=C1, merchant_id=M1, mechanic="stamps", before_held=7,
                         after_held=0, gained=1, target=8, rewards_earned=1)
    pro = make("pro")
    assert au.after_accrual(pro, ev, today=date(2026, 11, 11)) == "ready"
    assert pro.rows("notification_deliveries")[0]["body"] == "Tu premio en Café Vecino: Un café"
    plain = make("starter")
    au.after_accrual(plain, ev, today=date(2026, 11, 11))
    assert plain.rows("notification_deliveries")[0]["body"].startswith("Completaste tu tarjeta")


# --------------------------------------------------------------------------- #
# Birthdays
# --------------------------------------------------------------------------- #
BDAY_NOW = utc(2026, 11, 19, 14, 0)          # 09:00 Ecuador


def _bday_db(tier="pro", month=11, day=19, settings=None, **over):
    s = {"birthday_on": True, "birthday_gift": "Un postre de la casa",
         "birthday_validity_days": 7, **(settings or {})}
    return FakeDB(
        merchant_notification_settings=[_settings_row(**s)],
        subscriptions=[{"merchant_id": M1, "tier": tier}],
        merchants=[{"id": M1, "business_name": "Café Vecino", "is_demo": False}],
        customers=[{"id": CU1, "merchant_id": M1, "birthday_month": month,
                    "birthday_day": day},
                   {"id": CU2, "merchant_id": M1, "birthday_month": 1, "birthday_day": 1}],
        cards=[{"id": C1, "merchant_id": M1, "customer_id": CU1, "program_id": P1,
                "language": "es"},
               {"id": C2, "merchant_id": M1, "customer_id": CU1, "program_id": P2,
                "language": "es"},
               {"id": C3, "merchant_id": M1, "customer_id": CU2, "program_id": P1,
                "language": "es"}],
        notifiable_cards=[
            {"card_id": C1, "merchant_id": M1, "program_id": P1,
             "program_type": "loyalty_stamps"},
            {"card_id": C2, "merchant_id": M1, "program_id": P2,
             "program_type": "loyalty_points"},
            {"card_id": C3, "merchant_id": M1, "program_id": P1,
             "program_type": "loyalty_stamps"}],
        card_last_activity=[
            {"card_id": C1, "merchant_id": M1, "last_at": "2026-11-01T10:00:00+00:00"},
            {"card_id": C2, "merchant_id": M1, "last_at": "2026-11-15T10:00:00+00:00"},
            {"card_id": C3, "merchant_id": M1, "last_at": "2026-11-15T10:00:00+00:00"}],
        **over)


def test_birthday_issues_reward_on_most_recently_used_card():
    db = _bday_db()
    totals = au.run_birthdays(db, BDAY_NOW)
    assert totals == {"merchants": 1, "issued": 1, "queued": 1, "errors": 0}
    bc = db.rows("birthday_cards")[0]
    assert bc["card_id"] == C2 and bc["program_id"] == P2          # last used card wins
    assert (bc["customer_id"], bc["merchant_id"], bc["year"]) == (CU1, M1, 2026)
    assert bc["status"] == "issued" and bc["benefit"] == "Un postre de la casa"
    assert bc["valid_until"] == "2026-11-25"                        # today + 7 - 1
    assert bc["code"].startswith("CUMPLE-") and len(bc["code"]) == len("CUMPLE-XXXX")
    d = db.rows("notification_deliveries")[0]
    assert d["card_id"] == C2
    assert d["body"] == ("Feliz cumpleaños. Te regalamos un postre de la casa, "
                         "válido 7 días en Café Vecino.")
    assert d["dedupe_key"] == f"birthday:{CU1}:2026"
    assert datetime.fromisoformat(d["display_until"]) == BDAY_NOW + timedelta(hours=48)
    assert db.rows("notifications")[0]["type"] == "birthday"


def test_birthday_discount_text():
    db = _bday_db(settings={"birthday_benefit": "discount", "birthday_discount_percent": 20,
                            "birthday_validity_days": 1, "birthday_gift": None})
    au.run_birthdays(db, BDAY_NOW)
    assert db.rows("birthday_cards")[0]["benefit"] == "20 % de descuento"
    assert db.rows("birthday_cards")[0]["valid_until"] == "2026-11-19"
    assert db.rows("notification_deliveries")[0]["body"] == (
        "Feliz cumpleaños. Te regalamos un 20 % de descuento, válido 1 día en Café Vecino.")


def test_birthday_is_idempotent_per_customer_and_year():
    db = _bday_db()
    au.run_birthdays(db, BDAY_NOW)
    again = au.run_birthdays(db, BDAY_NOW + timedelta(minutes=5))
    assert again["issued"] == 0 and again["queued"] == 0
    assert len(db.rows("birthday_cards")) == 1
    assert len(db.rows("notification_deliveries")) == 1
    # next year is a new reward
    au.run_birthdays(db, utc(2027, 11, 19, 14))
    assert len(db.rows("birthday_cards")) == 2


def test_birthday_retries_the_notice_when_only_the_card_was_issued():
    db = _bday_db()
    au.run_birthdays(db, BDAY_NOW)
    db.rows("notification_deliveries").clear()                     # the enqueue was lost
    again = au.run_birthdays(db, BDAY_NOW + timedelta(minutes=5))
    assert again["issued"] == 0 and again["queued"] == 1
    assert len(db.rows("birthday_cards")) == 1


def test_birthday_custom_text_pro_placeholders():
    db = _bday_db(settings={"custom_texts": {"birthday": "Feliz día, {negocio} te da {premio}"}})
    au.run_birthdays(db, BDAY_NOW)
    assert db.rows("notification_deliveries")[0]["body"] == (
        "Feliz día, Café Vecino te da Un postre de la casa")   # {premio} as typed


@pytest.mark.parametrize("now,month,day,fires", [
    (utc(2027, 2, 28, 14), 2, 29, True),       # non-leap: Feb 29 falls on Feb 28
    (utc(2027, 2, 28, 14), 2, 28, True),
    (utc(2028, 2, 28, 14), 2, 29, False),      # leap year: they wait for Feb 29
    (utc(2028, 2, 28, 14), 2, 28, True),
    (utc(2028, 2, 29, 14), 2, 29, True),
    (utc(2027, 3, 1, 14), 2, 29, False),
])
def test_birthday_feb_29(now, month, day, fires):
    db = _bday_db(month=month, day=day)
    assert au.run_birthdays(db, now)["issued"] == (1 if fires else 0)


def test_birthday_uses_the_ecuador_day():
    # 03:00 UTC on the 20th is still the 19th in Ecuador (UTC-5)
    db = _bday_db()
    assert au.run_birthdays(db, utc(2026, 11, 20, 3, 0))["issued"] == 1
    assert au.run_birthdays(_bday_db(), utc(2026, 11, 19, 3, 0))["issued"] == 0


def test_birthday_skips_unusable_merchants_and_cards():
    assert au.run_birthdays(_bday_db(tier="growth"), BDAY_NOW)["merchants"] == 0
    off = _bday_db(settings={"birthday_on": False})
    assert au.run_birthdays(off, BDAY_NOW)["merchants"] == 0
    demo = _bday_db()
    demo.rows("merchants")[0]["is_demo"] = True
    assert au.run_birthdays(demo, BDAY_NOW)["merchants"] == 0
    assert demo.rows("birthday_cards") == []
    nogift = _bday_db(settings={"birthday_gift": None})
    assert au.run_birthdays(nogift, BDAY_NOW)["issued"] == 0
    # no notifiable loyalty card (paused / never saved) -> nothing to attach it to
    nocard = _bday_db()
    nocard.rows("notifiable_cards").clear()
    assert au.run_birthdays(nocard, BDAY_NOW)["issued"] == 0
    # a discount card is not a loyalty card
    for r in nocard.rows("notifiable_cards"):
        r["program_type"] = "seasonal_discount"
    nocard.rows("notifiable_cards").append({"card_id": C1, "merchant_id": M1, "program_id": P1,
                                            "program_type": "seasonal_discount"})
    assert au.run_birthdays(nocard, BDAY_NOW)["issued"] == 0


def test_birthday_failure_is_isolated(monkeypatch):
    db = _bday_db()

    def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(au, "run_birthday_for_merchant", boom)
    assert au.run_birthdays(db, BDAY_NOW)["errors"] == 1


def test_birthday_codes_are_unique_looking(monkeypatch):
    codes = iter(["CUMPLE-AAAA", "CUMPLE-AAAA", "CUMPLE-BBBB"])
    monkeypatch.setattr(au, "new_birthday_code", lambda: next(codes))
    db = _bday_db()
    db.rows("birthday_cards").append({"id": "old", "customer_id": "other", "year": 2025,
                                      "code": "CUMPLE-AAAA"})
    real_insert = db.insert

    def insert(name, row):
        if name == "birthday_cards" and any(
                r.get("code") == row.get("code") for r in db.rows(name)):
            raise RuntimeError("duplicate code")
        return real_insert(name, row)
    db.insert = insert
    au.run_birthdays(db, BDAY_NOW)
    assert [r["code"] for r in db.rows("birthday_cards")] == ["CUMPLE-AAAA", "CUMPLE-BBBB"]


# --------------------------------------------------------------------------- #
# 48 h display window
# --------------------------------------------------------------------------- #
def test_enqueue_event_and_timed_set_display_until():
    db = FakeDB()
    now = utc(2026, 11, 11, 15)
    outbox.enqueue_event(db, merchant_id=M1, card_id=C1, type_="ready", header="C",
                         body="b", day=date(2026, 11, 11), now=now)
    outbox.enqueue_timed(db, merchant_id=M1, type_="inactive", day=date(2026, 11, 11),
                         header="C", now=now,
                         rows=[{"card_id": C2, "body": "b", "dedupe_key": "k1"}])
    for d in db.rows("notification_deliveries"):
        assert datetime.fromisoformat(d["display_until"]) == now + timedelta(hours=48)


def test_dispatch_moves_automation_display_until_to_send_time():
    db = _dispatch_db((C1, {"display_until": (NOW + timedelta(hours=2)).isoformat()}),
                      source="automation")
    run(db, Wallet())
    d = next(r for r in db.rows("notification_deliveries") if r["id"] == "d0")
    assert d["status"] == "sent"
    assert datetime.fromisoformat(d["display_until"]) == NOW + timedelta(hours=48)


def test_dispatch_sends_the_48h_window_to_the_wallet():
    db = _dispatch_db((C1, {}), source="automation")
    seen = []

    def send(client, card_id, message, *, notify=True):
        seen.append(message.display_until)
    run(db, send)
    assert seen == [NOW + timedelta(hours=48)]


def test_dispatch_leaves_manual_display_until_alone():
    flash = (NOW + timedelta(hours=6)).isoformat()
    db = _dispatch_db((C1, {"display_until": flash}), (C2, {}), source="manual")
    run(db, Wallet())
    by = {r["id"]: r for r in db.rows("notification_deliveries")}
    assert by["d0"]["display_until"] == flash
    assert by["d1"]["display_until"] is None


def test_expired_automation_is_skipped_after_a_long_defer():
    db = _dispatch_db((C1, {"display_until": (NOW - timedelta(minutes=1)).isoformat()}),
                      source="automation")
    wallet = Wallet()
    run(db, wallet)
    assert wallet.calls == []
    assert next(r for r in db.rows("notification_deliveries")
                if r["id"] == "d0")["status"] == "skipped"


# --------------------------------------------------------------------------- #
# Notifier jobs: order and fault isolation
# --------------------------------------------------------------------------- #
def _notifier(monkeypatch, fail=()):
    from services.notifier import handler as n

    calls = []

    def step(name, value=0):
        def run_step(*a):
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} down")
            return value
        return run_step

    monkeypatch.setattr(n, "service_client", lambda: "db")
    monkeypatch.setattr(n.dispatch, "run_dispatch", step("dispatch", {}))
    monkeypatch.setattr(n.automations, "run_daily", step("daily", {}))
    monkeypatch.setattr(n.automations, "run_birthdays", step("birthdays", {}))
    monkeypatch.setattr(n.demo, "expire_demo_cards", step("demo_cards"))
    monkeypatch.setattr(n.demo, "purge_demo_campaigns", step("purge"))
    monkeypatch.setattr(n.wallet_saved, "poll_unsaved", step("saved"))
    monkeypatch.setattr(n.card_lifecycle, "pause_inactive_cards", step("pause"))
    monkeypatch.setattr(n.billing, "apply_scheduled_plan_changes", step("plans"))
    return n, calls


def test_notifier_job_order(monkeypatch):
    n, calls = _notifier(monkeypatch)
    n.handler({"job": "dispatch"}, None)
    assert calls == ["purge", "saved", "dispatch"]
    calls.clear()
    n.handler({"job": "daily"}, None)
    assert calls == ["demo_cards", "pause", "plans", "daily", "dispatch"]
    calls.clear()
    n.handler({"job": "birthday"}, None)
    assert calls == ["birthdays", "dispatch"]
    n.handler({}, None)                                  # no job = the minute dispatch
    with pytest.raises(ValueError):
        n.handler({"job": "bogus"}, None)


def test_notifier_daily_survives_every_failing_step(monkeypatch):
    n, calls = _notifier(monkeypatch, fail=("demo_cards", "pause", "plans", "daily"))
    result = n.handler({"job": "daily"}, None)
    assert calls == ["demo_cards", "pause", "plans", "daily", "dispatch"]
    assert result["paused"] == {"error": "pause down"}
    assert result["daily"] == {"error": "daily down"}
    assert result["dispatch"] == {}


def test_notifier_minute_job_survives_purge_and_poll_failures(monkeypatch):
    n, calls = _notifier(monkeypatch, fail=("purge", "saved"))
    result = n.handler({"job": "dispatch"}, None)
    assert calls == ["purge", "saved", "dispatch"]
    assert result["demo_purge"] == {"error": "purge down"}
    assert result["wallet_saved"] == {"error": "saved down"}


def test_notifier_birthday_job_dispatches_even_if_birthdays_fail(monkeypatch):
    n, calls = _notifier(monkeypatch, fail=("birthdays",))
    result = n.handler({"job": "birthday"}, None)
    assert calls == ["birthdays", "dispatch"]
    assert result["birthdays"] == {"error": "birthdays down"}
