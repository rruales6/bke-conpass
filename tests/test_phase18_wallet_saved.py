"""Phase 18 (D21): never-saved detection — the poller marks cards Google reports as saved."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from conpass_common import wallet_saved
from conpass_common.providers.google_wallet import GoogleWalletProvider

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters, self.payload, self.orders, self.n = [], None, [], None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r, c=col, v=val: r.get(c) >= v)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r, c=col, v=tuple(vals): r.get(c) in v)
        return self

    def order(self, col, desc=False, nullsfirst=None):
        self.orders.append((col, desc, nullsfirst))
        return self

    def limit(self, n):
        self.n = n
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def execute(self):
        rows = [r for r in self.db[self.table] if all(f(r) for f in self.filters)]
        if self.payload is not None:
            for r in rows:
                r.update(self.payload)
            self.db.setdefault("_updates", []).append((self.payload, [r["id"] for r in rows]))
            return type("R", (), {"data": rows})()
        for col, desc, nullsfirst in reversed(self.orders):
            rows.sort(key=lambda r, c=col: (r.get(c) is None, r.get(c) or ""), reverse=desc)
            if nullsfirst:
                rows.sort(key=lambda r, c=col: r.get(c) is not None)
        return type("R", (), {"data": rows[: self.n] if self.n else rows})()


class _Client:
    def __init__(self, cards):
        self.db = {"cards": cards}

    def table(self, name):
        return _Query(self.db, name)


def _card(i, *, installed=False, active=True, days_ago=1, checked=None):
    return {"id": f"c{i}", "active": active, "wallet_installed": installed,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
            "wallet_checked_at": checked}


class _Provider:
    def __init__(self, saved):
        self.saved, self.asked = saved, []

    def saved_cards(self, ids):
        self.asked.append(list(ids))
        return {i: self.saved[i] for i in ids if i in self.saved}


def test_marks_saved_and_stamps_checked():
    client = _Client([_card(1), _card(2), _card(3)])
    provider = _Provider({"c1": True, "c2": False})           # c3: unreadable this time
    assert wallet_saved.poll_unsaved(client, now=NOW, provider=provider) == 1
    cards = {c["id"]: c for c in client.db["cards"]}
    assert cards["c1"]["wallet_installed"] is True
    assert cards["c2"]["wallet_installed"] is False
    assert cards["c1"]["wallet_checked_at"] == cards["c2"]["wallet_checked_at"] == NOW.isoformat()
    assert cards["c3"]["wallet_checked_at"] is None           # retried first next time


def test_skips_installed_inactive_and_old_cards():
    client = _Client([_card(1, installed=True), _card(2, active=False), _card(3, days_ago=45),
                      _card(4)])
    provider = _Provider({"c4": True})
    assert wallet_saved.poll_unsaved(client, now=NOW, provider=provider) == 1
    assert provider.asked == [["c4"]]


def test_rotation_never_checked_first():
    client = _Client([_card(1, checked=(NOW - timedelta(minutes=1)).isoformat()), _card(2)])
    provider = _Provider({})
    wallet_saved.poll_unsaved(client, limit=1, now=NOW, provider=provider)
    assert provider.asked == [["c2"]]


def test_provider_failure_is_swallowed():
    class Boom:
        def saved_cards(self, ids):
            raise RuntimeError("not configured")
    client = _Client([_card(1)])
    assert wallet_saved.poll_unsaved(client, now=NOW, provider=Boom()) == 0
    assert "_updates" not in client.db


def test_nothing_to_poll():
    assert wallet_saved.poll_unsaved(_Client([]), now=NOW, provider=_Provider({})) == 0


def test_google_saved_cards_maps_has_users(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        card = request.url.path.rsplit(".card_", 1)[1]
        if card == "a":
            return httpx.Response(200, json={"hasUsers": True})
        if card == "b":
            return httpx.Response(200, json={})
        if card == "c":
            return httpx.Response(404)
        return httpx.Response(500)
    provider = GoogleWalletProvider.__new__(GoogleWalletProvider)
    provider._issuer_id = "999"
    monkeypatch.setattr(provider, "_api", lambda: httpx.Client(
        base_url="https://wallet.test", transport=httpx.MockTransport(handler)))
    assert provider.saved_cards(["a", "b", "c", "d"]) == {"a": True, "b": False, "c": False}


# --- Signed birthday link on the back of the pass (D15: never the opaque token) -------------
def test_birthday_link_token_signs_and_verifies(monkeypatch):
    from types import SimpleNamespace

    from conpass_common import card_links
    monkeypatch.setattr(card_links, "settings", SimpleNamespace(
        supabase_secret_key="sb_secret_test", public_app_url="https://console.conpass.cards"))
    tok = card_links.birthday_link_token("card-1")
    assert len(tok) == 24 and card_links.verify_birthday_link_token("card-1", tok)
    assert not card_links.verify_birthday_link_token("card-2", tok)
    assert not card_links.verify_birthday_link_token("card-1", "TOK")
    url = card_links.birthday_link_url("card-1", "prog-9")
    assert url == f"https://console.conpass.cards/tarjeta/card-1?p=prog-9&t={tok}"
    monkeypatch.setattr(card_links, "settings", SimpleNamespace(
        supabase_secret_key="", public_app_url="https://x"))
    assert card_links.birthday_link_url("card-1", "prog-9") is None
    assert not card_links.verify_birthday_link_token("card-1", tok)


def test_google_object_carries_birthday_link_and_clears_it():
    from conpass_common.providers.wallet import PassContent
    provider = GoogleWalletProvider.__new__(GoogleWalletProvider)
    provider._issuer_id = "999"
    base = dict(card_id="c1", program_id="p1", merchant_id="m1", program_type="loyalty_stamps",
                program_name="Club", merchant_name="Café", holder_name=None, opaque_token="TOK",
                stamps_for_reward=8)
    link = "https://x/tarjeta/c1?t=s"
    with_link = provider._object_payload(PassContent(**base, birthday_url=link))
    assert with_link["linksModuleData"]["uris"][0]["uri"] == "https://x/tarjeta/c1?t=s"
    assert "TOK" not in with_link["linksModuleData"]["uris"][0]["uri"]
    assert "linksModuleData" not in provider._object_payload(PassContent(**base))
