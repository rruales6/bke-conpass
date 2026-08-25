"""Phase 12 tests — card language (D16) and customer email reaching the wallet pass on
every path that builds one, plus the programs service's batched customer fetch and its
once-per-edit `sync_program` call. All network is mocked; sibling to test_wallet.py
(Phase 4), which owns the wallet-provider payload tests themselves. Uses monkeypatch so
patches never leak across modules."""
from __future__ import annotations

from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.idempotency import InMemoryIdempotencyStore
from conpass_common.providers.wallet import IssuedPass, WalletKind
from fastapi.testclient import TestClient

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"
CARD1 = "44444444-4444-4444-4444-444444444444"
CUST1 = "66666666-6666-6666-6666-666666666666"
CUST2 = "77777777-7777-7777-7777-777777777777"


def _identity(app, monkeypatch, roles=("merchant_owner",), merchant_id=M1):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# Enrollment — language persisted + echoed, customer email reaches the pass
# --------------------------------------------------------------------------- #
class _FakeEnrollRepo:
    program = {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Café",
               "mechanic": "stamps", "stamps_for_reward": 8, "reward": "Gratis",
               "welcome_bonus": 0, "active": True, "color": "#0EA5E9"}
    merchant = {"id": M1, "business_name": "Conpass QA"}

    def __init__(self, existing_card=None, existing_customer=None):
        self._existing = existing_card
        self._existing_customer = existing_customer
        self.created_card: dict | None = None

    def get_program(self, pid):
        return self.program

    def get_merchant(self, mid):
        return self.merchant

    def find_card_by_dedupe(self, pid, key):
        return self._existing

    def get_customer(self, cid):
        return self._existing_customer

    def create_customer(self, data):
        return {"id": CUST1, "email": data.get("email")}

    def create_card(self, data):
        self.created_card = data
        return {**data, "id": CARD1, "created_at": "2026-08-24T00:00:00Z",
               "stamps": 0, "points": 0}


class _CapturingProvider:
    """Records the PassContent every wallet call was built with, so tests can inspect
    the fields (language, member_email) that flow through from card/program/customer.
    Kept in three separate lists — `update` and `sync_program` are both card-less-vs-card
    calls that can share a card_id, so one combined list can't tell them apart."""

    def __init__(self):
        self.issued: list = []    # issue() / add_link()
        self.updated: list = []   # update()
        self.synced: list = []    # sync_program()

    def issue(self, content):
        self.issued.append(content)
        return IssuedPass(WalletKind.GOOGLE, add_link="https://pay.google.com/x")

    def add_link(self, content):
        self.issued.append(content)
        return "https://pay.google.com/x"

    def update(self, content):
        self.updated.append(content)

    def sync_program(self, content):
        self.synced.append(content)


def _seed_enroll(monkeypatch, repo, provider):
    from services.enrollment import handler as h
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_idempotency_store", lambda: InMemoryIdempotencyStore())
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)
    return h


def test_enroll_defaults_language_to_es_and_echoes_it(monkeypatch):
    repo = _FakeEnrollRepo()
    h = _seed_enroll(monkeypatch, repo, _CapturingProvider())
    r = TestClient(h.app).post(f"/programs/{P1}/enroll", json={"fullName": "Juan Ruiz"},
                               headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    assert repo.created_card["language"] == "es"
    assert r.json()["card"]["language"] == "es"


def test_enroll_persists_and_echoes_chosen_language(monkeypatch):
    repo = _FakeEnrollRepo()
    h = _seed_enroll(monkeypatch, repo, _CapturingProvider())
    r = TestClient(h.app).post(
        f"/programs/{P1}/enroll", json={"fullName": "Juan Ruiz", "language": "en"},
        headers={"Idempotency-Key": "k2"})
    assert r.status_code == 201
    assert repo.created_card["language"] == "en"
    assert r.json()["card"]["language"] == "en"


def test_enroll_passes_customer_email_to_the_pass(monkeypatch):
    repo = _FakeEnrollRepo()
    provider = _CapturingProvider()
    h = _seed_enroll(monkeypatch, repo, provider)
    r = TestClient(h.app).post(
        f"/programs/{P1}/enroll",
        json={"fullName": "Juan Ruiz", "email": "juan@example.com"},
        headers={"Idempotency-Key": "k3"})
    assert r.status_code == 201
    # This is the new-card path: build_pass_content got the customer just created,
    # with NO extra DB read (the repo has no get_customer override here to fall back on).
    assert provider.issued[-1].member_email == "juan@example.com"


def test_enroll_dedupe_replay_fetches_customer_for_the_pass(monkeypatch):
    existing_card = {"id": CARD1, "program_id": P1, "merchant_id": M1,
                     "customer_id": CUST1, "type": "loyalty_stamps",
                     "opaque_token": "TOK", "created_at": "2026-08-24T00:00:00Z",
                     "stamps": 0, "language": "es"}
    existing_customer = {"id": CUST1, "email": "juan@example.com"}
    repo = _FakeEnrollRepo(existing_card=existing_card, existing_customer=existing_customer)
    provider = _CapturingProvider()
    h = _seed_enroll(monkeypatch, repo, provider)
    r = TestClient(h.app).post(
        f"/programs/{P1}/enroll", json={"dedupeKey": "d1"},
        headers={"Idempotency-Key": "k4"})
    assert r.status_code == 200
    assert provider.issued[-1].member_email == "juan@example.com"


def test_enroll_dedupe_replay_without_customer_id_passes_none(monkeypatch):
    # Belt-and-suspenders for an old/anonymous card with no customer_id: no read is
    # attempted and the pass just goes without a contact line.
    existing_card = {"id": CARD1, "program_id": P1, "merchant_id": M1,
                     "type": "loyalty_stamps", "opaque_token": "TOK",
                     "created_at": "2026-08-24T00:00:00Z", "stamps": 0}
    repo = _FakeEnrollRepo(existing_card=existing_card)
    provider = _CapturingProvider()
    h = _seed_enroll(monkeypatch, repo, provider)
    r = TestClient(h.app).post(
        f"/programs/{P1}/enroll", json={"dedupeKey": "d1"},
        headers={"Idempotency-Key": "k5"})
    assert r.status_code == 200
    assert provider.issued[-1].member_email is None


# --------------------------------------------------------------------------- #
# Cards service — wallet-links picks up the customer's email
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, tables):
        self._t = tables

    def table(self, name):
        return _FakeQuery(self._t.get(name, []))


def test_cards_wallet_links_includes_customer_email(monkeypatch):
    from services.cards import handler as h
    tables = {
        "cards": [{"id": CARD1, "program_id": P1, "merchant_id": M1,
                   "customer_id": CUST1, "type": "loyalty_stamps", "opaque_token": "TOK",
                   "created_at": "2026-08-24T00:00:00Z"}],
        "programs": [{"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                      "name": "Café", "stamps_for_reward": 8}],
        "merchants": [{"id": M1, "business_name": "Conpass QA"}],
        "customers": [{"id": CUST1, "email": "juan@example.com"}],
    }
    monkeypatch.setattr(h, "service_client", lambda: _FakeClient(tables))
    provider = _CapturingProvider()
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)
    _identity(h.app, monkeypatch)
    r = TestClient(h.app).get(f"/cards/{CARD1}/wallet-links")
    assert r.status_code == 200
    assert provider.issued[-1].member_email == "juan@example.com"


def test_cards_wallet_links_skips_customer_query_without_customer_id(monkeypatch):
    from services.cards import handler as h
    tables = {
        "cards": [{"id": CARD1, "program_id": P1, "merchant_id": M1,
                   "type": "loyalty_stamps", "opaque_token": "TOK",
                   "created_at": "2026-08-24T00:00:00Z"}],  # no customer_id at all
        "programs": [{"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                      "name": "Café", "stamps_for_reward": 8}],
        "merchants": [{"id": M1, "business_name": "Conpass QA"}],
    }
    monkeypatch.setattr(h, "service_client", lambda: _FakeClient(tables))
    provider = _CapturingProvider()
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)
    _identity(h.app, monkeypatch)
    r = TestClient(h.app).get(f"/cards/{CARD1}/wallet-links")
    assert r.status_code == 200
    assert provider.issued[-1].member_email is None


# --------------------------------------------------------------------------- #
# Operations service — the post-commit wallet push carries the customer's email
# --------------------------------------------------------------------------- #
def test_push_wallet_update_includes_customer_email(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.card_rows[CARD1] = {"id": CARD1, "program_id": P1, "merchant_id": M1,
                             "customer_id": CUST1, "type": "loyalty_stamps",
                             "opaque_token": "TOK", "stamps": 3, "holder_name": "Juan"}
    repo.programs[P1] = {"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                        "name": "Café", "stamps_for_reward": 8}
    repo.merchants[M1] = {"id": M1, "business_name": "Conpass QA"}
    repo.customers[CUST1] = {"id": CUST1, "email": "juan@example.com"}
    provider = _CapturingProvider()
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)

    h._push_wallet_update(repo, CARD1)

    assert provider.updated[-1].member_email == "juan@example.com"


def test_push_wallet_update_without_customer_id_skips_the_read(monkeypatch):
    from services.operations import handler as h
    from services.operations.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.card_rows[CARD1] = {"id": CARD1, "program_id": P1, "merchant_id": M1,
                             "type": "loyalty_stamps", "opaque_token": "TOK", "stamps": 3}
    repo.programs[P1] = {"id": P1, "merchant_id": M1, "type": "loyalty_stamps",
                        "name": "Café", "stamps_for_reward": 8}
    repo.merchants[M1] = {"id": M1, "business_name": "Conpass QA"}
    provider = _CapturingProvider()
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)

    h._push_wallet_update(repo, CARD1)

    assert provider.updated[-1].member_email is None


# --------------------------------------------------------------------------- #
# Programs service — batched customer fetch + one sync_program per edit
# --------------------------------------------------------------------------- #
class _FakePushRepo:
    """Supports get/update (PATCH /programs) plus the wallet-push reads, with a batch
    get_customers_by_ids so tests can assert it is called once, not once per card."""

    def __init__(self, program, cards, customers):
        self._p = program
        self._cards = cards
        self._customers = customers
        self.get_customers_by_ids_calls: list[list[str]] = []

    def get(self, program_id):
        return self._p if self._p["id"] == program_id else None

    def update(self, program_id, patch):
        self._p.update(patch)
        return self._p

    def list_card_rows(self, program_id, limit):
        return self._cards[:limit]

    def count_cards(self, program_id):
        return len(self._cards)

    def get_merchant(self, merchant_id):
        return {"id": merchant_id, "business_name": "Café Vecino"}

    def get_customers_by_ids(self, customer_ids):
        self.get_customers_by_ids_calls.append(list(customer_ids))
        return {cid: c for cid, c in self._customers.items() if cid in customer_ids}


def _program_row(**overrides):
    return {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Club",
           "created_at": "2026-08-24T00:00:00Z", "color": "#112233",
           "icon_storage_key": None, "background_storage_key": None, **overrides}


def _card(card_id, customer_id=None):
    row = {"id": card_id, "merchant_id": M1, "program_id": P1, "opaque_token": "TOK",
          "stamps": 2, "points": 0, "holder_name": "Juan"}
    if customer_id:
        row["customer_id"] = customer_id
    return row


def _seed_push(monkeypatch, program, cards, customers, provider):
    import services.programs.handler as h
    repo = _FakePushRepo(program, cards, customers)
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    monkeypatch.setattr(h, "get_wallet_provider", lambda: provider)
    return h, repo


def test_program_push_batches_customer_fetch_in_one_call(monkeypatch):
    customers = {CUST1: {"id": CUST1, "email": "a@x.co"},
                CUST2: {"id": CUST2, "email": "b@x.co"}}
    cards = [_card("c1", CUST1), _card("c2", CUST2), _card("c3")]  # c3 has no customer
    provider = _CapturingProvider()
    h, repo = _seed_push(monkeypatch, _program_row(), cards, customers, provider)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/programs/{P1}", json={"appearance": {"color": "#654321"}})
    assert resp.status_code == 200

    assert len(repo.get_customers_by_ids_calls) == 1  # one query, not one per card
    assert sorted(repo.get_customers_by_ids_calls[0]) == sorted([CUST1, CUST2])

    emails = {c.card_id: c.member_email for c in provider.updated}
    assert emails == {"c1": "a@x.co", "c2": "b@x.co", "c3": None}


def test_program_edit_syncs_program_template_exactly_once(monkeypatch):
    cards = [_card("c1"), _card("c2"), _card("c3")]
    provider = _CapturingProvider()
    h, repo = _seed_push(monkeypatch, _program_row(), cards, {}, provider)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/programs/{P1}", json={"appearance": {"color": "#654321"}})
    assert resp.status_code == 200

    # Once, not once per card: the class-level template is per-program, `update` is per-card.
    assert len(provider.synced) == 1
    assert provider.synced[0].program_id == P1
    assert [c.card_id for c in provider.updated] == ["c1", "c2", "c3"]


def test_program_sync_failure_does_not_block_the_card_pushes(monkeypatch):
    cards = [_card("c1")]

    class _BoomOnSync(_CapturingProvider):
        def sync_program(self, content):
            raise RuntimeError("google is down")

    provider = _BoomOnSync()
    h, repo = _seed_push(monkeypatch, _program_row(), cards, {}, provider)
    _identity(h.app, monkeypatch)
    resp = TestClient(h.app).patch(
        f"/programs/{P1}", json={"appearance": {"color": "#654321"}})
    assert resp.status_code == 200
    assert [c.card_id for c in provider.updated] == ["c1"]  # update() still ran
