"""Signed per-card links (Phase 18): the birthday link on the back of the pass.

D15 keeps the card's opaque token (the QR value) off any visible text, so a link printed on the
pass cannot carry it. Instead it carries an HMAC of the card id, keyed with a secret derived
from the backend's service key. Knowing a card id is not enough to build one, and the link
authorises nothing beyond leaving a birthday.
"""
from __future__ import annotations

import hashlib
import hmac

from .config import settings

_LABEL = b"conpass/birthday-link/v1"


def _key() -> bytes:
    secret = (settings.supabase_secret_key or "").encode()
    if not secret:
        raise RuntimeError("no signing secret configured")
    return hmac.new(secret, _LABEL, hashlib.sha256).digest()


def birthday_link_token(card_id: str) -> str:
    """URL-safe signature for /tarjeta/<card_id>?t=<token> (first 24 hex chars = 96 bits)."""
    return hmac.new(_key(), str(card_id).encode(), hashlib.sha256).hexdigest()[:24]


def verify_birthday_link_token(card_id: str, token: str) -> bool:
    try:
        expected = birthday_link_token(card_id)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, (token or "").strip().lower())


def birthday_link_url(card_id: str, program_id: str) -> str | None:
    """/tarjeta/<card>?p=<program>&t=<signature> — the program id is needed by
    PUT /programs/{programId}/enroll/birthday; only the signature authorises."""
    try:
        token = birthday_link_token(card_id)
    except RuntimeError:
        return None
    return f"{settings.public_app_url}/tarjeta/{card_id}?p={program_id}&t={token}"
