"""Phase 16 — wallet side of notifications: back-of-pass messages, nearby locations,
level / balance-expiry modules, addMessage + quota, and wallet_sync's DB derivation.

No network: the provider's HTTP client is replaced, and wallet_sync runs against an
in-memory fake (tests marked real_wallet_sync opt out of the conftest isolation)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conpass_common import wallet_sync
from conpass_common.providers.wallet import PassContent, PassMessage, WalletQuotaExceeded

from tests._fake_supabase import FakeClient, _Query
from tests.test_wallet import CARD1, ISSUER, M1, P1, _fake_sa

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", ISSUER)
    monkeypatch.setenv("GOOGLE_WALLET_SA_JSON", _fake_sa())
    from conpass_common.providers.google_wallet import GoogleWalletProvider
    return GoogleWalletProvider()


def _content(**over) -> PassContent:
    base = dict(card_id=CARD1, program_id=P1, merchant_id=M1,
                program_type="loyalty_stamps", program_name="Café",
                merchant_name="Café Vecino", holder_name="Ana", opaque_token="TOK",
                stamps=6, stamps_for_reward=8, rewards_available=1, rewards_redeemed=1)
    base.update(over)
    return PassContent(**base)


# --- payloads -------------------------------------------------------------------------
def test_object_always_carries_messages_and_locations(provider):
    obj = provider._object_payload(_content())
    # Present even when empty: the object is PUT whole, an omitted list would be wiped.
    assert obj["messages"] == [] and obj["merchantLocations"] == []
    cls = provider._class_payload(_content())
    assert cls["merchantLocations"] == []


def test_messages_are_escaped_linked_and_expire(provider):
    until = NOW + timedelta(hours=3)
    msg = PassMessage(id="d1", header="Café Vecino", body="2x1 <b>hoy</b> & mañana",
                      url="https://cafe.example/menu?a=1&b=2", display_until=until)
    obj = provider._object_payload(_content(messages=(msg,), language="es"))
    m = obj["messages"][0]
    assert m["id"] == "d1" and m["header"] == "Café Vecino"
    assert m["messageType"] == "TEXT"          # re-sent on PUT → never re-notifies
    assert "&lt;b&gt;hoy&lt;/b&gt; &amp; mañana" in m["body"]
    assert '<a href="https://cafe.example/menu?a=1&amp;b=2">Más información</a>' in m["body"]
    assert m["displayInterval"]["end"]["date"] == until.isoformat()


def test_lists_are_trimmed_to_google_limits(provider):
    msgs = tuple(PassMessage(id=f"d{i}", header="h", body="b") for i in range(14))
    pts = tuple((-0.1 * i, -78.4) for i in range(14))
    obj = provider._object_payload(_content(messages=msgs, card_locations=pts))
    assert len(obj["messages"]) == 10 and len(obj["merchantLocations"]) == 10
    cls = provider._class_payload(_content(merchant_locations=pts))
    assert len(cls["merchantLocations"]) == 10
    assert cls["merchantLocations"][0] == {"latitude": -0.0, "longitude": -78.4}


def test_level_and_expiry_modules_on_the_back(provider):
    mods = {m["id"]: m for m in provider._text_modules(
        _content(level=3, balance_expires_on="2026-12-04", language="es"))}
    assert mods["lvl"] == {"id": "lvl", "header": "NIVEL", "body": "3"}
    assert mods["exp"]["header"] == "SALDO VENCE" and mods["exp"]["body"] == "4 dic 2026"
    en = {m["id"]: m for m in provider._text_modules(
        _content(level=2, balance_expires_on="2026-01-09", language="en"))}
    assert en["lvl"]["header"] == "LEVEL" and en["exp"]["body"] == "9 Jan 2026"
    # Google shows at most 10 text modules from the object.
    assert len(provider._text_modules(_content(level=3, balance_expires_on="2026-12-04",
                                               reward_text="Café"))) <= 10


def test_no_level_or_expiry_without_values(provider):
    ids = {m["id"] for m in provider._text_modules(_content())}
    assert "lvl" not in ids and "exp" not in ids


# --- addMessage -----------------------------------------------------------------------
class _Api:
    def __init__(self, status=200, text="{}"):
        self.calls, self.status, self.text = [], status, text

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, path, json=None):
        self.calls.append((path, json))
        return httpx.Response(self.status, text=self.text,
                              request=httpx.Request("POST", "https://x" + path))


def test_send_message_notifies_via_add_message(provider, monkeypatch):
    api = _Api()
    monkeypatch.setattr(provider, "_api", lambda: api)
    provider.send_message(_content(), PassMessage(id="d9", header="H", body="B"))
    path, payload = api.calls[0]
    assert path == f"/genericObject/{ISSUER}.card_{CARD1}/addMessage"
    assert payload["message"]["messageType"] == "TEXT_AND_NOTIFY"
    provider.send_message(_content(), PassMessage(id="d9", header="H", body="B"), notify=False)
    assert api.calls[1][1]["message"]["messageType"] == "TEXT"


@pytest.mark.parametrize("status,text", [
    (429, "{}"),
    (400, json.dumps({"error": {"message": "QuotaExceededException: too many"}})),
])
def test_send_message_quota_raises_typed_error(provider, monkeypatch, status, text):
    monkeypatch.setattr(provider, "_api", lambda: _Api(status, text))
    with pytest.raises(WalletQuotaExceeded):
        provider.send_message(_content(), PassMessage(id="d1", header="H", body="B"))


def test_send_message_other_errors_raise_http(provider, monkeypatch):
    monkeypatch.setattr(provider, "_api", lambda: _Api(500, "boom"))
    with pytest.raises(httpx.HTTPStatusError):
        provider.send_message(_content(), PassMessage(id="d1", header="H", body="B"))


# --- wallet_sync derivation (real lookups, fake client) --------------------------------
class _Q(_Query):
    """Adds the `.not_.is_(col, "null")` chain the per-card locations query uses."""

    @property
    def not_(self):
        return self

    def is_(self, col, _null):
        self._rows = [r for r in self._rows if r.get(col) is not None]
        return self


class _Client(FakeClient):
    def table(self, name):
        return _Q(self.tables, name)


L1, L2, L3 = "loc-1", "loc-2", "loc-3"


def _db(**over):
    tables = {
        "subscriptions": [{"merchant_id": M1, "tier": "growth"}],
        "merchant_locations": [
            {"id": L1, "merchant_id": M1, "latitude": -0.18065, "longitude": -78.48443,
             "active": True, "created_at": "2026-01-01"},
            {"id": L2, "merchant_id": M1, "latitude": -0.20102, "longitude": -78.43085,
             "active": True, "created_at": "2026-01-02"},
            {"id": L3, "merchant_id": M1, "latitude": -0.17, "longitude": -78.47,
             "active": False, "created_at": "2026-01-03"},
        ],
        "merchant_notification_settings": [],
        "notification_deliveries": [],
        "transactions": [],
        "card_last_activity": [],
    }
    tables.update(over)
    return _Client(tables)


CARD = {"id": CARD1, "merchant_id": M1, "program_id": P1, "opaque_token": "TOK",
        "stamps": 6, "points": 0, "rewards_available": 1, "rewards_redeemed": 1,
        "enrolled_location_id": L2, "language": "es"}
PROGRAM = {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Café",
           "stamps_for_reward": 8}


@pytest.mark.real_wallet_sync
def test_context_sends_active_locales_only_on_nearby_plans():
    ctx = wallet_sync.merchant_context(_db(), M1)
    assert ctx.nearby and ctx.class_locations == ((-0.18065, -78.48443), (-0.20102, -78.43085))
    starter = wallet_sync.merchant_context(
        _db(subscriptions=[{"merchant_id": M1, "tier": "starter"}]), M1)
    assert not starter.nearby and starter.class_locations == ()


@pytest.mark.real_wallet_sync
def test_content_derives_level_messages_locations_and_expiry():
    client = _db(
        merchant_notification_settings=[{"merchant_id": M1, "card_locations_enrolled": True,
                                         "card_locations_top": True,
                                         "balance_expiry_days": 30}],
        transactions=[{"card_id": CARD1, "location_id": L1}] * 3
        + [{"card_id": CARD1, "location_id": L3}, {"card_id": CARD1, "location_id": None}],
        notification_deliveries=[
            {"id": "old", "card_id": CARD1, "status": "sent", "header": "h", "body": "flash",
             "url": None, "display_until": (NOW - timedelta(hours=1)).isoformat(),
             "sent_at": "2026-11-10T10:00:00+00:00"},
            {"id": "new", "card_id": CARD1, "status": "sent_silent", "header": "h",
             "body": "hola", "url": None, "display_until": None,
             "sent_at": "2026-11-11T10:00:00+00:00"},
            {"id": "pend", "card_id": CARD1, "status": "pending", "header": "h",
             "body": "x", "url": None, "display_until": None, "sent_at": None},
        ],
        card_last_activity=[{"card_id": CARD1, "last_at": "2026-11-01T20:00:00+00:00"}],
    )
    c = wallet_sync.content_for(client, CARD, PROGRAM, {"business_name": "Café Vecino"},
                                None, now=NOW)
    assert c.level == 3                                   # 1 + 1 available + 1 redeemed
    assert [m.id for m in c.messages] == ["new"]          # expired + unsent dropped
    # enrolment locale first, then most-scanned active one; paused L3 never sent
    assert c.card_locations == ((-0.20102, -78.43085), (-0.18065, -78.48443))
    assert c.merchant_locations == ((-0.18065, -78.48443), (-0.20102, -78.43085))
    assert c.balance_expires_on == "2026-12-01"           # last visit + 30 d, Ecuador date


@pytest.mark.real_wallet_sync
def test_no_expiry_date_without_window_or_balance():
    client = _db(card_last_activity=[{"card_id": CARD1, "last_at": "2026-11-01T00:00:00Z"}])
    assert wallet_sync.content_for(client, CARD, PROGRAM, None, None,
                                   now=NOW).balance_expires_on is None
    client = _db(merchant_notification_settings=[{"merchant_id": M1,
                                                  "balance_expiry_days": 30}],
                 card_last_activity=[{"card_id": CARD1, "last_at": "2026-11-01T00:00:00Z"}])
    empty = dict(CARD, stamps=0)
    assert wallet_sync.content_for(client, empty, PROGRAM, None, None,
                                   now=NOW).balance_expires_on is None


@pytest.mark.real_wallet_sync
def test_program_content_needs_no_card():
    content = wallet_sync.program_content(_db(), PROGRAM, {"business_name": "Café Vecino"})
    assert content.program_id == P1 and len(content.merchant_locations) == 2


@pytest.mark.real_wallet_sync
def test_sync_merchant_classes_is_best_effort(monkeypatch):
    calls = []

    class _P:
        def sync_program(self, content):
            calls.append(content.program_id)
            if content.program_id == "p-bad":
                raise RuntimeError("boom")

    client = _db(programs=[dict(PROGRAM, id="p-bad"), dict(PROGRAM)],
                 merchants=[{"id": M1, "business_name": "Café Vecino"}])
    monkeypatch.setattr(wallet_sync, "get_wallet_provider", lambda: _P())
    wallet_sync.sync_merchant_classes(client, M1)      # must not raise
    assert calls == ["p-bad", P1]


@pytest.mark.real_wallet_sync
def test_send_card_message_reads_one_row(monkeypatch):
    sent = []

    class _P:
        def send_message(self, content, message, notify=True):
            sent.append((content.card_id, content.language, message.id, notify))

    monkeypatch.setattr(wallet_sync, "get_wallet_provider", lambda: _P())
    client = _db(cards=[dict(CARD, language="en")])
    wallet_sync.send_card_message(client, CARD1, PassMessage(id="d1", header="h", body="b"),
                                  notify=False)
    assert sent == [(CARD1, "en", "d1", False)]
    with pytest.raises(LookupError):
        wallet_sync.send_card_message(client, "nope", PassMessage(id="d", header="h", body="b"))


def test_send_message_missing_object_is_typed(provider, monkeypatch):
    from conpass_common.providers.wallet import WalletObjectMissing
    monkeypatch.setattr(provider, "_api", lambda: _Api(404, "not found"))
    with pytest.raises(WalletObjectMissing):
        provider.send_message(_content(), PassMessage(id="d1", header="H", body="B"))


def test_dispatch_skips_cards_without_a_wallet_object():
    from conpass_common.notify import dispatch
    from conpass_common.providers.wallet import WalletObjectMissing

    updates = []

    class _C:
        def table(self, _name):
            class _T:
                def update(self, fields):
                    updates.append(fields)
                    return self

                def eq(self, *_):
                    return self

                def execute(self):
                    return None
            return _T()

    def send(*_a, **_k):
        raise WalletObjectMissing("nope")

    status = dispatch.deliver(_C(), {"id": "d1", "card_id": "c1", "header": "h", "body": "b",
                                     "attempts": 1}, send=send, now_fn=lambda: NOW)
    assert status == "skipped" and updates[-1]["status"] == "skipped"
    assert updates[-1]["error"].startswith("no_wallet_object")
