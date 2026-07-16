"""Supabase access.

Backend Lambdas use the SERVICE ROLE key and thus bypass RLS — tenant checks are
enforced in code via the caller's Identity. The client is created lazily so importing
this module never requires credentials (offline-friendly).
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from .config import settings
from .errors import NotConfigured

if TYPE_CHECKING:  # avoid importing supabase at cold start unless used
    from supabase import Client


@lru_cache(maxsize=1)
def service_client() -> Client:
    """Backend Data API client using the SECRET key (service_role, bypasses RLS).

    All backend DML goes through the Supabase Data API (PostgREST) via this client.
    """
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise NotConfigured(
            "Supabase is not configured. Add to secrets.yaml under conpass.supabase:\n"
            "  url: <project url>\n"
            "  publishable_key: <sb_publishable_…>\n"
            "  secret_key: <sb_secret_…>   # backend only\n"
            "  jwks_url: <…/auth/v1/.well-known/jwks.json>\n"
            "  db_url: <postgres connection string>"
        )
    from supabase import create_client  # local import keeps cold start lean

    return create_client(settings.supabase_url, settings.supabase_secret_key)
