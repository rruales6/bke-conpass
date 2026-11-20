"""Card helpers shared by enrollment and the demo seed."""
from __future__ import annotations

import secrets


def new_opaque_token() -> str:
    """The per-card secret carried in the pass QR and accepted by POST /operations/resolve."""
    return secrets.token_urlsafe(16)


def card_flags(row: dict) -> dict:
    """The Phase 18 Card fields every card output carries: `level` (persisted, min 1) and
    `paused` (paused_at is set)."""
    return {"level": max(1, int(row.get("level") or 1)), "paused": bool(row.get("paused_at"))}
