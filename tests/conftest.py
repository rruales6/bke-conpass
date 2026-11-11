"""Safety net: unit tests must never reach the live Supabase project.

The developer's environment has real credentials, and several code paths (best-effort
wallet push, notification enqueue after an accrual) call `service_client()` themselves.
Without this, a test that forgets to fake it would silently query production data. Tests
that need a client monkeypatch `service_client` in the handler module; the live
integration module (skipped unless asked for) is exempt.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _no_live_supabase(request, monkeypatch):
    if request.module.__name__.endswith("test_live_integration"):
        yield          # a generator fixture must yield even when it does nothing
        return
    from conpass_common import db

    db.service_client.cache_clear()
    monkeypatch.setattr(db, "settings", SimpleNamespace(supabase_url="", supabase_secret_key=""))
    yield
    db.service_client.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_wallet_sync(request, monkeypatch):
    """wallet_sync reads settings, locales, sent messages and last activity every time a
    pass is built. Handler tests fake only the rows the handler itself reads, so by default
    those lookups return "nothing extra" and the pass is built exactly as before Phase 16.
    Tests of wallet_sync itself opt out with `@pytest.mark.real_wallet_sync` and pass a
    fake client."""
    if (request.node.get_closest_marker("real_wallet_sync")
            or request.module.__name__.endswith("test_live_integration")):
        return
    from conpass_common import wallet_sync
    from conpass_common.notify.settings import MerchantNotificationSettings

    empty = wallet_sync.MerchantContext(MerchantNotificationSettings(), False, (), {})
    monkeypatch.setattr(wallet_sync, "merchant_context", lambda client, merchant_id: empty)
    monkeypatch.setattr(wallet_sync, "_card_messages", lambda client, card_id, now: ())
    monkeypatch.setattr(wallet_sync, "_card_locations", lambda client, card, ctx: ())
    monkeypatch.setattr(wallet_sync, "_balance_expires_on",
                        lambda client, card, program, ctx: None)
