"""Phase 18 (D21) — enrollment: consent default, birthday day+month, the tokenized
birthday PUT, and level / paused on every Card output (enrollment + cards service)."""
from __future__ import annotations

import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.idempotency import InMemoryIdempotencyStore
from fastapi.testclient import TestClient

from services.cards import handler as cards_h
from services.enrollment import handler as h
from tests.fakedb import FakeDB

M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
P2 = "33333333-3333-3333-3333-333333333334"
CARD1 = "44444444-4444-4444-4444-444444444444"
CARD2 = "44444444-4444-4444-4444-444444444445"
CUST1 = "77777777-7777-7777-7777-777777777777"
TOKEN = "s3cret-token-ABC"


class Repo:
    program = {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Café",
               "mechanic": "stamps", "stamps_for_reward": 8, "welcome_bonus": 0,
               "active": True}

    def __init__(self):
        self.customers: dict[str, dict] = {CUST1: {"id": CUST1, "merchant_id": M1}}
        self.cards: dict[str, dict] = {
            CARD1: {"id": CARD1, "program_id": P1, "merchant_id": M1, "customer_id": CUST1,
                    "type": "loyalty_stamps", "opaque_token": TOKEN,
                    "created_at": "2026-11-19T00:00:00Z"},
            CARD2: {"id": CARD2, "program_id": P1, "merchant_id": M1, "customer_id": None,
                    "type": "loyalty_stamps", "opaque_token": "other-token",
                    "created_at": "2026-11-19T00:00:00Z"}}
        self.created_customer: dict | None = None
        self.created_card: dict | None = None

    def get_program(self, pid):
        return self.program

    def get_merchant(self, mid):
        return {"id": mid, "business_name": "QA"}

    def find_card_by_dedupe(self, pid, key):
        return None

    def get_customer(self, cid):
        return self.customers.get(cid)

    def get_card(self, card_id):
        return self.cards.get(card_id)

    def create_customer(self, data):
        self.created_customer = {"id": "88888888-8888-8888-8888-888888888888", **data}
        self.customers[self.created_customer["id"]] = self.created_customer
        return self.created_customer

    def create_card(self, data):
        self.created_card = data
        return {**data, "id": CARD1, "created_at": "2026-11-19T00:00:00Z", "stamps": 0,
                "points": 0, "level": 1}

    def update_customer(self, cid, patch):
        self.customers[cid].update(patch)

    def link_customer(self, card_id, cid):
        self.cards[card_id]["customer_id"] = cid


class NoWallet:
    def issue(self, content):
        raise RuntimeError("no wallet in tests")   # degrades to no links

    add_link = issue


@pytest.fixture
def repo(monkeypatch):
    r = Repo()
    monkeypatch.setattr(h, "get_repo", lambda: r)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: InMemoryIdempotencyStore())
    monkeypatch.setattr(h, "get_wallet_provider", lambda: NoWallet())
    return r


def _enroll(body, key="k1"):
    return TestClient(h.app).post(f"/programs/{P1}/enroll", json=body,
                                  headers={"Idempotency-Key": key})


def _put(repo_unused, **body):
    return TestClient(h.app).put(f"/programs/{P1}/enroll/birthday", json=body)


def _bday(day, month, *, card=CARD1, token=TOKEN):
    return {"cardId": card, "token": token, "day": day, "month": month}


# --------------------------------------------------------------------------- #
# POST /programs/{id}/enroll
# --------------------------------------------------------------------------- #
def test_enroll_consent_absent_is_false(repo):
    assert _enroll({"fullName": "Ana"}).status_code == 201
    assert repo.created_customer["consent"] is False


def test_enroll_consent_true_is_kept(repo):
    _enroll({"fullName": "Ana", "consent": True}, key="k2")
    assert repo.created_customer["consent"] is True


def test_enroll_birthday_keeps_day_and_month_only(repo):
    _enroll({"fullName": "Ana", "birthday": "1990-03-14"}, key="k3")
    c = repo.created_customer
    assert (c["birthday_day"], c["birthday_month"]) == (14, 3)
    assert "birthday" not in c and "1990" not in str(c)


def test_enroll_without_birthday_stores_none(repo):
    _enroll({"fullName": "Ana"}, key="k4")
    assert repo.created_customer["birthday_day"] is None
    assert repo.created_customer["birthday_month"] is None


def test_enroll_card_output_has_level_and_paused(repo):
    card = _enroll({"fullName": "Ana"}, key="k5").json()["card"]
    assert card["level"] == 1 and card["paused"] is False


def test_enroll_replay_card_reports_level_and_pause(repo, monkeypatch):
    existing = {**repo.cards[CARD1], "level": 4, "paused_at": "2026-06-01T00:00:00+00:00",
                "stamps": 3}
    monkeypatch.setattr(repo, "find_card_by_dedupe", lambda pid, key: existing)
    r = _enroll({"dedupeKey": "d"}, key="k6")
    assert r.status_code == 200
    assert r.json()["card"]["level"] == 4 and r.json()["card"]["paused"] is True


# --------------------------------------------------------------------------- #
# PUT /programs/{id}/enroll/birthday
# --------------------------------------------------------------------------- #
def test_birthday_set_updates_the_customer(repo):
    r = _put(repo, **_bday(14, 3))
    assert r.status_code == 204 and r.content == b""
    assert (repo.customers[CUST1]["birthday_day"], repo.customers[CUST1]["birthday_month"]) \
        == (14, 3)


def test_birthday_clear_with_both_null(repo):
    _put(repo, **_bday(14, 3))
    assert _put(repo, **_bday(None, None)).status_code == 204
    assert repo.customers[CUST1]["birthday_day"] is None
    assert repo.customers[CUST1]["birthday_month"] is None


@pytest.mark.parametrize("day,month", [(14, None), (None, 3)])
def test_birthday_one_null_is_422(repo, day, month):
    assert _put(repo, **_bday(day, month)).status_code == 422
    assert "birthday_day" not in repo.customers[CUST1]


@pytest.mark.parametrize("day,month", [(31, 4), (30, 2), (31, 6), (32, 1), (0, 1), (5, 13)])
def test_birthday_impossible_dates_are_422(repo, day, month):
    assert _put(repo, **_bday(day, month)).status_code == 422


@pytest.mark.parametrize("day,month", [(29, 2), (31, 1), (30, 4), (1, 1), (31, 12)])
def test_birthday_possible_dates_accepted_including_feb_29(repo, day, month):
    assert _put(repo, **_bday(day, month)).status_code == 204


def test_birthday_wrong_token_unknown_card_or_foreign_program_are_the_same_404(repo):
    wrong = _put(repo, **_bday(1, 1, token="nope"))
    unknown = _put(repo, **_bday(1, 1, card="44444444-4444-4444-4444-4444444444ff"))
    other = TestClient(h.app).put(f"/programs/{P2}/enroll/birthday", json=_bday(1, 1))
    assert wrong.status_code == unknown.status_code == other.status_code == 404
    assert wrong.json() == unknown.json() == other.json()
    assert "birthday_day" not in repo.customers[CUST1]


def test_birthday_for_card_without_customer_creates_and_links_one(repo):
    r = _put(repo, **_bday(9, 12, card=CARD2, token="other-token"))
    assert r.status_code == 204
    created = repo.created_customer
    assert created["merchant_id"] == M1 and created["consent"] is False
    assert (created["birthday_day"], created["birthday_month"]) == (9, 12)
    assert repo.cards[CARD2]["customer_id"] == created["id"]


def test_birthday_clear_for_card_without_customer_creates_nothing(repo):
    assert _put(repo, **_bday(None, None, card=CARD2, token="other-token")).status_code == 204
    assert repo.created_customer is None


def test_birthday_token_is_compared_in_constant_time(repo, monkeypatch):
    calls = []
    real = h.secrets.compare_digest
    monkeypatch.setattr(h.secrets, "compare_digest",
                        lambda a, b: calls.append(1) or real(a, b))
    _put(repo, **_bday(1, 1))
    assert calls


# --------------------------------------------------------------------------- #
# cards service
# --------------------------------------------------------------------------- #
def test_cards_service_card_has_level_and_paused(monkeypatch):
    db = FakeDB(cards=[{
        "id": CARD1, "program_id": P1, "merchant_id": M1, "type": "loyalty_stamps",
        "opaque_token": "t", "stamps": 1, "level": 5,
        "paused_at": "2026-06-01T00:00:00+00:00", "created_at": "2026-11-19T00:00:00Z"}])
    monkeypatch.setattr(cards_h, "_client", lambda: db)
    monkeypatch.setitem(cards_h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id="u", email="a@b.co", roles=["merchant_owner"], merchant_id=M1))
    body = TestClient(cards_h.app).get(f"/cards/{CARD1}").json()
    assert body["level"] == 5 and body["paused"] is True
    db.rows("cards")[0].update(paused_at=None, level=None)
    body = TestClient(cards_h.app).get(f"/cards/{CARD1}").json()
    assert body["level"] == 1 and body["paused"] is False
    cards_h.app.dependency_overrides.clear()
