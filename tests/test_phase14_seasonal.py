"""Phase 14 — seasonal campaigns: campaign status, program create/patch rules, enrolment
gating, the one-use discount at the cashier, and the aligned plan catalog. Everything is
in-memory (fake repos, in-memory idempotency store); no network."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from conpass_common import campaigns, plans
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.campaigns import campaign_fields, campaign_status
from conpass_common.idempotency import InMemoryIdempotencyStore
from conpass_common.providers.wallet import IssuedPass, WalletKind
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
CARD1 = "44444444-4444-4444-4444-444444444444"
OP1 = "55555555-5555-5555-5555-555555555555"

D = date(2026, 5, 10)    # fixed "today" for the pure status tests
FROM, UNTIL = date(2026, 5, 1), date(2026, 5, 12)


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


def _seasonal(**over):
    return {"id": P1, "merchant_id": M1, "type": "seasonal_discount", "name": "Madres",
            "active": True, "valid_from": FROM.isoformat(), "valid_until": UNTIL.isoformat(),
            "discount_percent": 20, "created_at": "2026-04-01T00:00:00Z", **over}


# --------------------------------------------------------------------------- #
# campaign_status / campaign_fields
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("today,expected", [
    (FROM - timedelta(days=1), "upcoming"),
    (FROM, "active"),                         # validFrom inclusive
    (D, "active"),
    (UNTIL, "active"),                        # validUntil inclusive
    (UNTIL + timedelta(days=1), "ended"),
])
def test_seasonal_status_window_edges(today, expected):
    assert campaign_status(_seasonal(), today) == expected


def test_status_accepts_dates_or_iso_strings():
    as_dates = _seasonal(valid_from=FROM, valid_until=UNTIL)
    assert campaign_status(as_dates, D) == "active"
    assert campaign_status(as_dates, UNTIL + timedelta(days=1)) == "ended"
    assert campaign_status(_seasonal(), D) == "active"


def test_disabled_wins_over_every_window():
    for today in (FROM - timedelta(days=5), D, UNTIL + timedelta(days=5)):
        assert campaign_status(_seasonal(active=False), today) == "disabled"
    assert campaign_status({"type": "loyalty_stamps", "active": False}, D) == "disabled"


def test_non_seasonal_is_active_whatever_the_date_columns_say():
    assert campaign_status({"type": "loyalty_stamps", "active": True}, D) == "active"
    stale = {"type": "loyalty_points", "valid_until": "2000-01-01"}   # ignored
    assert campaign_status(stale, D) == "active"
    assert campaign_status({"type": "membership_pass"}, D) == "active"


def test_ecuador_clock_is_fixed_utc_minus_5():
    assert timezone(timedelta(hours=-5)) == campaigns.ECUADOR_TZ
    # 03:00 UTC on the 11th is still 22:00 on the 10th in Ecuador.
    assert datetime(2026, 5, 11, 3, 0, tzinfo=UTC).astimezone(
        campaigns.ECUADOR_TZ).date() == date(2026, 5, 10)
    assert isinstance(campaigns.today_ec(), date)


def test_campaign_fields_seasonal_and_plain():
    out = campaign_fields(_seasonal(occasion="Día de la Madre", reward=None), D)
    assert out == {"occasion": "Día de la Madre", "discountPercent": 20,
                   "validFrom": "2026-05-01", "validUntil": "2026-05-12",
                   "campaignStatus": "active"}
    # No None values; every program carries a status.
    plain = campaign_fields({"type": "loyalty_stamps", "occasion": None,
                             "discount_percent": None, "active": True}, D)
    assert plain == {"campaignStatus": "active"}


# --------------------------------------------------------------------------- #
# POST / PATCH /programs
# --------------------------------------------------------------------------- #
class _ProgRepo:
    def __init__(self, existing=None):
        self.rows = {r["id"]: r for r in (existing or [])}
        self.created: list[dict] = []

    def create(self, data):
        row = {"id": P1, "created_at": "2026-10-20T00:00:00Z", **data}
        self.created.append(row)
        self.rows[row["id"]] = row
        return row

    def get(self, pid):
        return self.rows.get(pid)

    def list(self, mid):
        return list(self.rows.values())

    def update(self, pid, patch):
        self.rows[pid].update(patch)
        return self.rows[pid]

    def list_card_rows(self, *_a):
        return []

    def count_cards(self, *_a):
        return 0

    def get_merchant(self, mid):
        return {"id": mid, "business_name": "Café"}

    def get_customers_by_ids(self, ids):
        return {}


def _programs(monkeypatch, repo):
    from services.programs import handler as h
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _identity(h.app, monkeypatch)
    return h


def _create(**over):
    body = {"type": "seasonal_discount", "name": "Madres", "occasion": "Día de la Madre",
            "discountPercent": 20, "validFrom": "2026-05-01", "validUntil": "2026-05-12",
            "appearance": {"color": "#112233"}, "wallets": ["google"]}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def test_create_seasonal_persists_fields_with_null_mechanic(monkeypatch):
    repo = _ProgRepo()
    h = _programs(monkeypatch, repo)
    # A stray mechanic / inactivityDays must not leak onto a seasonal row.
    r = TestClient(h.app).post("/programs", json=_create(
        mechanic="stamps", inactivityDays=30))
    assert r.status_code == 201, r.text
    row = repo.created[0]
    assert row["type"] == "seasonal_discount" and row["mechanic"] is None
    assert row["inactivity_days"] is None
    assert (row["occasion"], row["discount_percent"]) == ("Día de la Madre", 20)
    assert (row["valid_from"], row["valid_until"]) == ("2026-05-01", "2026-05-12")
    body = r.json()
    assert body["validFrom"] == "2026-05-01" and body["validUntil"] == "2026-05-12"
    assert body["discountPercent"] == 20 and body["occasion"] == "Día de la Madre"
    assert body["campaignStatus"] in {"upcoming", "active", "ended"}
    assert "inactivityDays" not in body


@pytest.mark.parametrize("over,needle", [
    ({"validFrom": None}, "validFrom"),
    ({"validUntil": None}, "validUntil"),
    ({"validFrom": "2026-05-12", "validUntil": "2026-05-01"}, "on or after"),
    ({"discountPercent": None}, "offer"),                       # no percent, no text
    ({"discountPercent": None, "reward": "   "}, "offer"),      # blank text isn't an offer
])
def test_create_seasonal_validation_422(monkeypatch, over, needle):
    repo = _ProgRepo()
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).post("/programs", json=_create(**over))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"
    assert needle in r.json()["message"]
    assert repo.created == []


def test_create_seasonal_single_day_and_either_kind_of_offer(monkeypatch):
    repo = _ProgRepo()
    h = _programs(monkeypatch, repo)
    c = TestClient(h.app)
    same_day = c.post("/programs", json=_create(validFrom="2026-05-12",
                                                validUntil="2026-05-12"))
    assert same_day.status_code == 201                                   # until == from ok
    percent_only = c.post("/programs", json=_create())
    text_only = c.post("/programs", json=_create(discountPercent=None, reward="2x1 postres"))
    assert percent_only.status_code == 201 and text_only.status_code == 201
    assert repo.created[2]["discount_percent"] is None
    assert repo.created[2]["reward"] == "2x1 postres"


def test_seasonal_needs_no_plan_feature(monkeypatch):
    """Available on every plan: create never consults the subscription tier."""
    repo = _ProgRepo()
    h = _programs(monkeypatch, repo)
    monkeypatch.setattr(h, "service_client", lambda: (_ for _ in ()).throw(
        AssertionError("no tier lookup expected")))
    assert TestClient(h.app).post("/programs", json=_create()).status_code == 201


def test_create_non_seasonal_ignores_seasonal_fields(monkeypatch):
    repo = _ProgRepo()
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).post("/programs", json=_create(
        type="loyalty_stamps", mechanic="stamps", stampsForReward=8, reward="Gratis"))
    assert r.status_code == 201, r.text
    row = repo.created[0]
    assert row["occasion"] is None and row["discount_percent"] is None
    assert row["valid_from"] is None and row["valid_until"] is None
    body = r.json()
    for k in ("occasion", "discountPercent", "validFrom", "validUntil"):
        assert k not in body
    assert body["campaignStatus"] == "active" and body["inactivityDays"] == 90


def test_list_and_get_carry_campaign_status_for_every_program(monkeypatch):
    repo = _ProgRepo([_seasonal(valid_until="2000-01-02", valid_from="2000-01-01"),
                      _seasonal(id="p2", type="loyalty_stamps", name="Club",
                                valid_from=None, valid_until=None, active=False)])
    h = _programs(monkeypatch, repo)
    c = TestClient(h.app)
    listed = {p["id"]: p["campaignStatus"] for p in c.get("/programs").json()}
    assert listed == {P1: "ended", "p2": "disabled"}
    assert c.get(f"/programs/{P1}").json()["campaignStatus"] == "ended"


def test_patch_seasonal_updates_fields_and_clears_occasion(monkeypatch):
    repo = _ProgRepo([_seasonal(occasion="Día de la Madre")])
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}", json={
        "occasion": "", "discountPercent": 30, "validUntil": "2026-05-20"})
    assert r.status_code == 200, r.text
    row = repo.rows[P1]
    assert row["occasion"] is None and row["discount_percent"] == 30
    assert row["valid_until"] == "2026-05-20" and row["valid_from"] == "2026-05-01"
    assert "occasion" not in r.json() and r.json()["validUntil"] == "2026-05-20"


@pytest.mark.parametrize("patch,needle", [
    ({"validUntil": "2026-04-30"}, "on or after"),          # merged: until < stored from
    ({"validFrom": "2026-06-01"}, "on or after"),           # merged: from > stored until
])
def test_patch_validates_the_merged_row_dates(monkeypatch, patch, needle):
    repo = _ProgRepo([_seasonal()])
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}", json=patch)
    assert r.status_code == 422 and needle in r.json()["message"]
    assert repo.rows[P1]["valid_until"] == "2026-05-12"       # nothing written


def test_patch_cannot_remove_the_last_offer(monkeypatch):
    # percent-only campaign: blanking the reward is fine (percent remains) ...
    repo = _ProgRepo([_seasonal(reward="Postre gratis")])
    h = _programs(monkeypatch, repo)
    c = TestClient(h.app)
    # text-only campaign: nothing left once the text is blanked.
    repo.rows[P1].update(discount_percent=None)
    r = c.patch(f"/programs/{P1}", json={"reward": " "})
    assert r.status_code == 422 and "offer" in r.json()["message"]
    # ... but adding a percentage at the same time makes the merged row valid.
    ok = c.patch(f"/programs/{P1}", json={"reward": "", "discountPercent": 15})
    assert ok.status_code == 200 and repo.rows[P1]["discount_percent"] == 15


def test_patch_seasonal_fields_ignored_for_other_types(monkeypatch):
    repo = _ProgRepo([_seasonal(type="loyalty_stamps", valid_from=None, valid_until=None,
                                discount_percent=None)])
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}", json={
        "occasion": "X", "discountPercent": 10, "validFrom": "2026-05-12",
        "validUntil": "2026-05-01", "name": "Nuevo"})        # inverted dates: not validated
    assert r.status_code == 200, r.text
    row = repo.rows[P1]
    assert row["name"] == "Nuevo"
    assert "occasion" not in row and row["discount_percent"] is None
    assert row["valid_from"] is None and row["valid_until"] is None


def test_patch_unrelated_field_does_not_revalidate_a_legacy_row(monkeypatch):
    repo = _ProgRepo([_seasonal(valid_from=None)])             # invalid on purpose
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}", json={"name": "Otro"})
    assert r.status_code == 200


def test_seasonal_columns_are_wallet_visible_and_trigger_the_push(monkeypatch):
    from services.programs import handler as h
    for col in ("occasion", "discount_percent", "valid_from", "valid_until"):
        assert col in h._WALLET_VISIBLE
    repo = _ProgRepo([_seasonal()])
    h = _programs(monkeypatch, repo)
    pushed: list = []
    monkeypatch.setattr(h, "_push_appearance_to_passes", lambda r, p: pushed.append(p["id"]))
    c = TestClient(h.app)
    assert c.patch(f"/programs/{P1}", json={"validUntil": "2026-05-20"}).status_code == 200
    assert pushed == [P1]
    c.patch(f"/programs/{P1}", json={"validUntil": "2026-05-20"})   # unchanged: no push
    assert pushed == [P1]


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #
class _Provider:
    def issue(self, content):
        return IssuedPass(WalletKind.GOOGLE, add_link="https://pay.google.com/x")

    def add_link(self, content):
        return "https://pay.google.com/x"

    def update(self, content): ...
    def sync_program(self, content): ...


class _EnrollRepo:
    merchant = {"id": M1, "business_name": "Conpass QA"}

    def __init__(self, program, existing_card=None):
        self.program = program
        self.existing = existing_card
        self.created_card: dict | None = None
        self.customers = 0

    def get_program(self, pid):
        return self.program

    def get_merchant(self, mid):
        return self.merchant

    def find_card_by_dedupe(self, pid, key):
        return self.existing

    def get_customer(self, cid):
        return None

    def create_customer(self, data):
        self.customers += 1
        return {"id": "cust-1", "email": data.get("email")}

    def create_card(self, data):
        self.created_card = data
        return {"stamps": 0, "points": 0, "rewards_available": 0, **data, "id": CARD1,
                "created_at": "2026-08-24T00:00:00Z"}


def _enroll(monkeypatch, repo):
    from services.enrollment import handler as h
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: InMemoryIdempotencyStore())
    monkeypatch.setattr(h, "get_wallet_provider", lambda: _Provider())
    return TestClient(h.app)


def _window(start_offset, end_offset):
    today = campaigns.today_ec()
    return {"valid_from": (today + timedelta(days=start_offset)).isoformat(),
            "valid_until": (today + timedelta(days=end_offset)).isoformat()}


def _post_enroll(client, **extra):
    return client.post(f"/programs/{P1}/enroll", json={"fullName": "Juan Ruiz", **extra},
                       headers={"Idempotency-Key": str(uuid.uuid4())})


def test_enroll_seasonal_issues_card_with_one_discount(monkeypatch):
    repo = _EnrollRepo(_seasonal(**_window(-1, 3)))
    r = _post_enroll(_enroll(monkeypatch, repo))
    assert r.status_code == 201, r.text
    assert repo.created_card["rewards_available"] == 1
    assert repo.created_card["type"] == "seasonal_discount"
    bal = r.json()["card"]["balance"]
    assert bal["rewardsAvailable"] == 1 and bal["rewardsRedeemed"] == 0
    assert bal["stamps"] == 0 and bal["points"] == 0


def test_enroll_non_seasonal_card_starts_with_no_rewards(monkeypatch):
    repo = _EnrollRepo({"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "C",
                        "mechanic": "stamps", "stamps_for_reward": 8, "active": True})
    r = _post_enroll(_enroll(monkeypatch, repo))
    assert r.status_code == 201 and repo.created_card["rewards_available"] == 0


@pytest.mark.parametrize("window,status", [
    ((2, 5), "upcoming"),
    ((-9, -2), "ended"),
])
def test_enroll_refused_outside_the_window(monkeypatch, window, status):
    prog = _seasonal(**_window(*window))
    repo = _EnrollRepo(prog)
    r = _post_enroll(_enroll(monkeypatch, repo))
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "campaign_not_active"
    assert body["details"] == {"status": status, "validFrom": prog["valid_from"],
                               "validUntil": prog["valid_until"]}
    assert repo.customers == 0 and repo.created_card is None   # nothing created


def test_enroll_refused_when_disabled_for_any_type(monkeypatch):
    seasonal = _EnrollRepo(_seasonal(active=False, **_window(-1, 3)))
    r = _post_enroll(_enroll(monkeypatch, seasonal))
    assert r.status_code == 422 and r.json()["code"] == "campaign_not_active"
    assert r.json()["details"]["status"] == "disabled"

    stamps = _EnrollRepo({"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                          "name": "C", "active": False})
    r = _post_enroll(_enroll(monkeypatch, stamps))
    assert r.status_code == 422 and r.json()["details"] == {"status": "disabled"}


def test_enroll_missing_program_is_still_404(monkeypatch):
    r = _post_enroll(_enroll(monkeypatch, _EnrollRepo(None)))
    assert r.status_code == 404


def test_dedupe_replay_returns_existing_card_after_the_campaign_ended(monkeypatch):
    existing = {"id": CARD1, "program_id": P1, "merchant_id": M1, "type": "seasonal_discount",
                "opaque_token": "TOK", "rewards_available": 1,
                "created_at": "2026-05-02T00:00:00Z"}
    repo = _EnrollRepo(_seasonal(**_window(-9, -2)), existing_card=existing)
    r = _post_enroll(_enroll(monkeypatch, repo), dedupeKey="device-1")
    assert r.status_code == 200 and r.json()["card"]["id"] == CARD1
    assert repo.customers == 0
    # ... but a different device (no existing card) is refused.
    repo.existing = None
    assert _post_enroll(_enroll(monkeypatch, repo), dedupeKey="device-2").status_code == 422


def test_dedupe_replay_of_a_disabled_campaign_is_refused(monkeypatch):
    existing = {"id": CARD1, "program_id": P1, "merchant_id": M1, "type": "seasonal_discount",
                "opaque_token": "TOK", "created_at": "2026-05-02T00:00:00Z"}
    repo = _EnrollRepo(_seasonal(active=False, **_window(-1, 3)), existing_card=existing)
    r = _post_enroll(_enroll(monkeypatch, repo), dedupeKey="device-1")
    assert r.status_code == 422 and r.json()["details"]["status"] == "disabled"


# --------------------------------------------------------------------------- #
# Operations: redeem (apply discount) / accrue / resolve
# --------------------------------------------------------------------------- #
def _ops(monkeypatch, program, *, available=1, redeemed=0):
    from services.operations import handler as h
    from services.operations.logic import CardState, ProgramRules
    from services.operations.repository import CardRow, InMemoryRepository

    repo = InMemoryRepository()
    repo.programs[P1] = program
    repo.rules[P1] = ProgramRules("seasonal_discount", None, None, None, None)
    repo.cards[CARD1] = CardRow(
        id=CARD1, program_id=P1, merchant_id=M1, program_type="seasonal_discount",
        state=CardState(stamps=0, points=0, rewards_available=available, active=True,
                        rewards_redeemed=redeemed))
    store = InMemoryIdempotencyStore()
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: store)
    _identity(h.app, monkeypatch, roles=("operation_user",))
    return TestClient(h.app), repo


def _redeem(client, key=None):
    return client.post("/operations/redeem", json={"cardId": CARD1, "operationUserId": OP1},
                       headers={"Idempotency-Key": key or str(uuid.uuid4())})


def test_redeem_seasonal_active_applies_the_discount_once(monkeypatch):
    client, repo = _ops(monkeypatch, _seasonal(**_window(-1, 3)))
    r = _redeem(client)
    assert r.status_code == 200, r.text
    bal = r.json()["card"]["balance"]
    assert bal["rewardsRedeemed"] == 1 and bal["rewardsAvailable"] == 0
    assert [t.kind for t in repo.txns] == ["redeem"]     # a normal redeem transaction

    again = _redeem(client)                              # fresh key: a second use
    assert again.status_code == 409
    assert again.json()["code"] == "discount_already_used"
    assert len(repo.txns) == 1


def test_redeem_seasonal_replay_of_first_apply_returns_stored_200(monkeypatch):
    client, repo = _ops(monkeypatch, _seasonal(**_window(-1, 3)))
    key = str(uuid.uuid4())
    first, replay = _redeem(client, key), _redeem(client, key)
    assert first.status_code == 200 and replay.status_code == 200   # not 409
    assert first.json() == replay.json()
    assert len(repo.txns) == 1 and repo.cards[CARD1].state.rewards_redeemed == 1


def test_redeem_seasonal_still_works_on_the_last_day(monkeypatch):
    client, _ = _ops(monkeypatch, _seasonal(**_window(-4, 0)))   # validUntil == today
    assert _redeem(client).status_code == 200


@pytest.mark.parametrize("window,status", [((2, 5), "upcoming"), ((-9, -1), "ended")])
def test_redeem_seasonal_outside_window_is_422(monkeypatch, window, status):
    prog = _seasonal(**_window(*window))
    client, repo = _ops(monkeypatch, prog)
    r = _redeem(client)
    assert r.status_code == 422
    assert r.json()["code"] == "campaign_not_active"
    assert r.json()["details"] == {"status": status, "validFrom": prog["valid_from"],
                                   "validUntil": prog["valid_until"]}
    assert repo.txns == [] and repo.cards[CARD1].state.rewards_available == 1


def test_redeem_seasonal_disabled_is_422(monkeypatch):
    client, _ = _ops(monkeypatch, _seasonal(active=False, **_window(-1, 3)))
    r = _redeem(client)
    assert r.status_code == 422 and r.json()["details"]["status"] == "disabled"


def test_redeem_status_check_precedes_the_already_used_check(monkeypatch):
    client, _ = _ops(monkeypatch, _seasonal(**_window(-9, -1)), available=0, redeemed=1)
    assert _redeem(client).json()["code"] == "campaign_not_active"


def test_redeem_non_seasonal_is_unchanged_even_with_stale_date_columns(monkeypatch):
    from services.operations import handler as h
    from services.operations.logic import CardState
    from services.operations.repository import CardRow

    client, repo = _ops(monkeypatch, _seasonal(type="loyalty_stamps", **_window(-9, -1)))
    repo.cards[CARD1] = CardRow(
        id=CARD1, program_id=P1, merchant_id=M1, program_type="loyalty_stamps",
        state=CardState(stamps=0, points=0, rewards_available=0, active=True))
    r = _redeem(client)
    assert r.status_code == 409 and r.json()["code"] == "conflict"   # not discount_*
    assert h.SEASONAL == "seasonal_discount"


def test_accrue_on_a_seasonal_card_is_422(monkeypatch):
    client, repo = _ops(monkeypatch, _seasonal(**_window(-1, 3)))
    for body in ({"stamps": 1}, {"amount": 10.0}):
        r = client.post("/operations/accrue",
                        json={"cardId": CARD1, "operationUserId": OP1, **body},
                        headers={"Idempotency-Key": str(uuid.uuid4())})
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "validation_error"
    assert repo.txns == []


def test_resolve_returns_seasonal_program_with_status(monkeypatch):
    client, repo = _ops(monkeypatch, _seasonal(occasion="San Valentín", **_window(-1, 3)))
    repo.card_rows[CARD1] = {"id": CARD1, "program_id": P1, "merchant_id": M1,
                             "type": "seasonal_discount", "opaque_token": "TOK",
                             "rewards_available": 1, "created_at": "2026-05-02T00:00:00Z"}
    r = client.post("/operations/resolve", json={"code": "TOK"})
    assert r.status_code == 200, r.text
    prog = r.json()["program"]
    assert prog["type"] == "seasonal_discount" and prog["campaignStatus"] == "active"
    assert prog["occasion"] == "San Valentín" and prog["discountPercent"] == 20
    assert "inactivityDays" not in prog
    assert r.json()["card"]["balance"]["rewardsAvailable"] == 1


# --------------------------------------------------------------------------- #
# Plan catalog (modelo-negocio v2.0) and billing
# --------------------------------------------------------------------------- #
def test_plan_catalog_matches_business_model_v2():
    starter = plans.PLANS["starter"]
    assert starter.included_notifications == 500
    assert str(starter.extra_notification_usd) == "0.015"
    assert starter.features[:4] == ("campaigns", "basic_notifications", "reports",
                                    "user_management")
    for tier in plans.TIER_ORDER:
        assert plans.PLANS[tier].has("campaigns") and plans.PLANS[tier].has(
            "basic_notifications")
    # Gating is unchanged: manual notification campaigns Growth+, automation Pro.
    assert plans.cheapest_with(plans.MANUAL_CAMPAIGNS) == "growth"
    assert plans.cheapest_with(plans.AUTOMATED_CAMPAIGNS) == "pro"


def test_billing_counts_read_the_active_passes_view():
    """Usage / admin figures must come from `merchant_active_passes` (which excludes the
    cards of ended seasonal campaigns), never from a raw `cards` count."""
    from services.admin.repository import AdminRepository
    from services.merchants.repository import MerchantsRepository

    class _C:
        def __init__(self):
            self.tables: list[str] = []

        def table(self, name):
            self.tables.append(name)
            return self

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            return type("R", (), {"data": [{"merchant_id": M1, "active_pass_count": 7}]})()

    c = _C()
    assert MerchantsRepository(c).active_card_count(M1) == 7
    assert AdminRepository(c).active_pass_count(M1) == 7
    assert AdminRepository(c).active_card_counts() == {M1: 7}
    assert set(c.tables) == {"merchant_active_passes"}
