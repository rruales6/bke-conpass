"""Phase 14 — abandonment window (`inactivityDays`), abandonment-rate metrics, the demo
sample card and the renamed wallet labels. Everything is in-memory: a small fake of the
Supabase Data API client (select/insert/update/delete with eq/order/limit) stands in for
the database, so no network is touched."""
from __future__ import annotations

import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.demo import (
    DEMO_CARD_DEDUPE_KEY,
    DEMO_CUSTOMER_EMAIL,
    DEMO_CUSTOMER_NAME,
    ensure_sample_card,
)
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
P2 = "44444444-4444-4444-4444-444444444444"
CARD = "55555555-5555-5555-5555-555555555555"


# --------------------------------------------------------------------------- #
# Minimal fake Data API client
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, db, name):
        self._db, self._name = db, name
        self._rows = list(db.tables.setdefault(name, []))
        self._op = "select"
        self._payload = None
        self._limit = None

    def select(self, *_a, **_k):
        return self

    def insert(self, data):
        self._op, self._payload = "insert", data
        return self

    def update(self, patch):
        self._op, self._payload = "update", patch
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def order(self, col, desc=False):
        self._rows = sorted(self._rows, key=lambda r: r.get(col) or "", reverse=desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        table = self._db.tables.setdefault(self._name, [])
        if self._op == "insert":
            row = {"id": f"{self._name}-{len(table) + 1}",
                   "created_at": f"2026-10-{len(table) + 1:02d}T00:00:00Z",
                   "active": True, **self._payload}
            table.append(row)
            self._db.writes += 1
            return type("R", (), {"data": [row]})()
        if self._op == "update":
            for r in self._rows:
                r.update(self._payload)
            self._db.writes += 1
            return type("R", (), {"data": self._rows})()
        rows = self._rows[: self._limit] if self._limit else self._rows
        return type("R", (), {"data": rows})()


class _DB:
    def __init__(self, **tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.writes = 0

    def table(self, name):
        return _Q(self, name)


def _owner(app, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], merchant_id=M1))


def _create_body(**over):
    body = {
        "type": "loyalty_stamps", "name": "Club", "mechanic": "stamps",
        "stampsForReward": 8, "reward": "Gratis",
        "appearance": {"color": "#112233"}, "wallets": ["google"],
    }
    body.update(over)
    return body


class _CreateRepo:
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
    _owner(h.app, monkeypatch)
    return h


# --------------------------------------------------------------------------- #
# inactivityDays on create / patch / read
# --------------------------------------------------------------------------- #
def test_create_persists_inactivity_days_for_accumulation(monkeypatch):
    repo = _CreateRepo()
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).post("/programs", json=_create_body(inactivityDays=45))
    assert r.status_code == 201
    assert repo.created[0]["inactivity_days"] == 45
    assert r.json()["inactivityDays"] == 45


def test_create_without_inactivity_days_reports_default_90(monkeypatch):
    repo = _CreateRepo()
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).post("/programs", json=_create_body(type="loyalty_points",
                                                              mechanic="points"))
    assert r.status_code == 201
    assert repo.created[0]["inactivity_days"] is None   # NULL = default, not a stored 90
    assert r.json()["inactivityDays"] == 90


def test_create_membership_ignores_inactivity_days_and_omits_it(monkeypatch):
    repo = _CreateRepo()
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).post("/programs", json=_create_body(
        type="membership_pass", mechanic=None, stampsForReward=None, reward=None,
        membershipValidity="monthly", inactivityDays=30))
    assert r.status_code == 201, r.text
    assert repo.created[0]["inactivity_days"] is None
    assert "inactivityDays" not in r.json()


@pytest.mark.parametrize("bad", [0, 3651, -5])
def test_create_rejects_out_of_range_inactivity_days(monkeypatch, bad):
    h = _programs(monkeypatch, _CreateRepo())
    r = TestClient(h.app).post("/programs", json=_create_body(inactivityDays=bad))
    assert r.status_code == 422


def _row(**over):
    return {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Club",
            "created_at": "2026-10-20T00:00:00Z", **over}


def test_patch_updates_inactivity_days(monkeypatch):
    repo = _CreateRepo([_row()])
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}", json={"inactivityDays": 120})
    assert r.status_code == 200
    assert repo.rows[P1]["inactivity_days"] == 120
    assert r.json()["inactivityDays"] == 120


def test_patch_without_inactivity_days_leaves_it_unchanged(monkeypatch):
    repo = _CreateRepo([_row(inactivity_days=30)])
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}", json={"name": "Nuevo"})
    assert r.status_code == 200
    assert repo.rows[P1]["inactivity_days"] == 30
    assert r.json()["inactivityDays"] == 30


def test_patch_on_membership_ignores_inactivity_days(monkeypatch):
    repo = _CreateRepo([_row(type="membership_pass")])
    h = _programs(monkeypatch, repo)
    r = TestClient(h.app).patch(f"/programs/{P1}",
                                json={"inactivityDays": 10, "name": "Pase"})
    assert r.status_code == 200
    assert "inactivity_days" not in repo.rows[P1]
    assert "inactivityDays" not in r.json()


def test_list_and_get_surface_inactivity_days(monkeypatch):
    repo = _CreateRepo([_row(inactivity_days=15)])
    repo.list = lambda mid: list(repo.rows.values())
    h = _programs(monkeypatch, repo)
    c = TestClient(h.app)
    assert c.get("/programs").json()[0]["inactivityDays"] == 15
    assert c.get(f"/programs/{P1}").json()["inactivityDays"] == 15


# --------------------------------------------------------------------------- #
# Metrics: churnRate = abandonment, inactivityDays
# --------------------------------------------------------------------------- #
def _metrics(monkeypatch, program, view_rows):
    from services.programs import handler as h
    repo = _CreateRepo([program])
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "service_client", lambda: _DB(program_metrics_view=view_rows))
    _owner(h.app, monkeypatch)
    return TestClient(h.app).get(f"/programs/{program['id']}/metrics")


_VIEW = {"program_id": P1, "visits": 9, "redemptions": 2, "active_installed_passes": 7,
         "installs_this_week": 1, "eligible_for_reminder": 3, "second_visit_rate_30d": 50.0}


def test_metrics_maps_churn_and_inactivity_from_view(monkeypatch):
    r = _metrics(monkeypatch, _row(inactivity_days=30),
                 [{**_VIEW, "churn_rate": 25.5, "inactivity_days": 30}])
    assert r.json() == {
        "visits": 9, "redemptions": 2, "activeInstalledPasses": 7, "installsThisWeek": 1,
        "eligibleForReminder": 3, "secondVisitRate30d": 50.0,
        "churnRate": 25.5, "inactivityDays": 30,
    }


def test_metrics_zero_abandonment_is_kept_not_dropped(monkeypatch):
    body = _metrics(monkeypatch, _row(), [{**_VIEW, "churn_rate": 0, "inactivity_days": 90}]).json()
    assert body["churnRate"] == 0.0 and body["inactivityDays"] == 90


def test_metrics_membership_omits_churn_and_inactivity(monkeypatch):
    r = _metrics(monkeypatch, _row(type="membership_pass"),
                 [{**_VIEW, "churn_rate": None, "inactivity_days": None}])
    body = r.json()
    assert "churnRate" not in body and "inactivityDays" not in body
    assert body["visits"] == 9 and body["secondVisitRate30d"] == 50.0


def test_metrics_fallback_row_derives_from_program_type(monkeypatch):
    acc = _metrics(monkeypatch, _row(inactivity_days=45), []).json()
    assert acc["churnRate"] == 0.0 and acc["inactivityDays"] == 45
    assert acc["visits"] == 0
    default = _metrics(monkeypatch, _row(), []).json()
    assert default["inactivityDays"] == 90
    mem = _metrics(monkeypatch, _row(type="membership_pass"), []).json()
    assert "churnRate" not in mem and "inactivityDays" not in mem
    assert mem["visits"] == 0


# --------------------------------------------------------------------------- #
# Cashier resolution also returns the Program (with inactivityDays)
# --------------------------------------------------------------------------- #
def test_resolve_program_carries_inactivity_days(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.programs[P1] = _row(inactivity_days=21, stamps_for_reward=8, mechanic="stamps")
    repo.card_rows[CARD] = {"id": CARD, "program_id": P1, "merchant_id": M1,
                            "type": "loyalty_stamps", "opaque_token": "TOK",
                            "created_at": "2026-10-20T00:00:00Z"}
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _owner(h.app, monkeypatch)
    r = TestClient(h.app).post("/operations/resolve", json={"code": "TOK"})
    assert r.status_code == 200, r.text
    assert r.json()["program"]["inactivityDays"] == 21


# --------------------------------------------------------------------------- #
# Demo sample card
# --------------------------------------------------------------------------- #
def _demo_db(cards=(), with_customer=True):
    customers = ([{"id": "cust-1", "merchant_id": M1, "email": DEMO_CUSTOMER_EMAIL}]
                 if with_customer else [])
    return _DB(
        merchants=[{"id": M1, "business_name": "Demo", "is_demo": True,
                    "created_at": "2026-10-01T00:00:00Z"}],
        programs=[
            {"id": P1, "merchant_id": M1, "active": True, "created_at": "2026-10-02T00:00:00Z"},
            {"id": P2, "merchant_id": M1, "active": True, "created_at": "2026-10-03T00:00:00Z"},
        ],
        profiles=[{"merchant_id": M1, "role": "merchant_owner", "email": "o@x.co"}],
        customers=customers,
        cards=[dict(c) for c in cards],
    )


def _get_demo(monkeypatch, db):
    from services.merchants import handler as h
    from services.merchants.repository import MerchantsRepository
    monkeypatch.setattr(h, "get_repo", lambda: MerchantsRepository(db))
    return TestClient(h.app).get("/demo")


def _card_row(token, program_id, *, created="2026-10-05T00:00:00Z", active=True):
    return {"id": f"card-{token}", "merchant_id": M1, "customer_id": "cust-1",
            "program_id": program_id, "opaque_token": token, "created_at": created,
            "active": active}


def test_demo_includes_sample_card_code_of_latest_program(monkeypatch):
    db = _demo_db([_card_row("OLD", P1), _card_row("NEW", P2)])
    r = _get_demo(monkeypatch, db)
    assert r.status_code == 200
    assert r.json()["programId"] == P2
    assert r.json()["sampleCardCode"] == "NEW"
    assert db.writes == 0   # GET stays side-effect free


def test_demo_prefers_card_in_program_id_over_a_newer_card_elsewhere(monkeypatch):
    db = _demo_db([_card_row("IN-P2", P2, created="2026-10-04T00:00:00Z"),
                   _card_row("IN-P1", P1, created="2026-10-09T00:00:00Z")])
    assert _get_demo(monkeypatch, db).json()["sampleCardCode"] == "IN-P2"


def test_demo_falls_back_to_most_recent_active_card_in_another_program(monkeypatch):
    db = _demo_db([_card_row("A", P1, created="2026-10-04T00:00:00Z"),
                   _card_row("B", P1, created="2026-10-06T00:00:00Z"),
                   _card_row("GONE", P2, active=False)])
    assert _get_demo(monkeypatch, db).json()["sampleCardCode"] == "B"


def test_demo_omits_sample_card_code_without_card_or_customer(monkeypatch):
    for db in (_demo_db(), _demo_db(with_customer=False)):
        r = _get_demo(monkeypatch, db)
        assert r.status_code == 200
        assert "sampleCardCode" not in r.json()


def test_demo_ignores_cards_of_other_customers(monkeypatch):
    other = {**_card_row("STRANGER", P2), "customer_id": "someone-else"}
    assert "sampleCardCode" not in _get_demo(monkeypatch, _demo_db([other])).json()


# --------------------------------------------------------------------------- #
# ensure_sample_card (seed_demo / reset_demo)
# --------------------------------------------------------------------------- #
def _seed_db():
    return _DB(programs=[
        {"id": P1, "merchant_id": M1, "active": True, "type": "loyalty_stamps",
         "mechanic": "stamps", "welcome_bonus": 1, "created_at": "2026-10-02T00:00:00Z"},
        {"id": P2, "merchant_id": M1, "active": True, "type": "loyalty_stamps",
         "mechanic": "stamps", "welcome_bonus": 1, "created_at": "2026-10-03T00:00:00Z"},
    ])


def test_ensure_sample_card_creates_customer_and_card_in_latest_program():
    db = _seed_db()
    card = ensure_sample_card(db, M1)
    assert card["program_id"] == P2 and card["merchant_id"] == M1
    assert card["opaque_token"] and card["dedupe_key"] == DEMO_CARD_DEDUPE_KEY
    assert card["stamps"] == 1
    (customer,) = db.tables["customers"]
    assert customer["email"] == DEMO_CUSTOMER_EMAIL
    assert customer["full_name"] == DEMO_CUSTOMER_NAME
    assert card["customer_id"] == customer["id"]


def test_ensure_sample_card_is_idempotent():
    db = _seed_db()
    first = ensure_sample_card(db, M1)
    second = ensure_sample_card(db, M1)
    assert second["id"] == first["id"] and second["opaque_token"] == first["opaque_token"]
    assert len(db.tables["customers"]) == 1 and len(db.tables["cards"]) == 1


def test_ensure_sample_card_reactivates_an_inactive_card():
    db = _seed_db()
    first = ensure_sample_card(db, M1)
    db.tables["cards"][0]["active"] = False
    again = ensure_sample_card(db, M1)
    assert again["id"] == first["id"] and again["active"] is True
    assert len(db.tables["cards"]) == 1


def test_ensure_sample_card_without_a_program_does_nothing():
    db = _DB()
    assert ensure_sample_card(db, M1) is None
    assert db.writes == 0


# --------------------------------------------------------------------------- #
# Wallet labels
# --------------------------------------------------------------------------- #
def test_wallet_labels_use_fidelizacion_wording_and_short_reward_label():
    from conpass_common.providers.google_wallet import _LABELS

    assert _LABELS["es"]["loyalty_stamps"] == "Tarjeta de fidelización"
    assert _LABELS["es"]["loyalty_points"] == "Tarjeta de fidelización"
    assert _LABELS["en"]["loyalty_stamps"] == "Stamp card"
    assert _LABELS["en"]["loyalty_points"] == "Points card"
    assert _LABELS["en"]["reward"] == "REWARD"
    assert _LABELS["es"]["reward"] == "RECOMPENSA"
    assert not any("lealtad" in v.lower() for v in _LABELS["es"].values())
    assert not any("program" in v.lower() for v in _LABELS["en"].values())
