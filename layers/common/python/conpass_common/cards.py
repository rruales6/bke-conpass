"""Card helpers shared by enrollment and the demo seed."""
from __future__ import annotations

import secrets


def new_opaque_token() -> str:
    """The per-card secret carried in the pass QR and accepted by POST /operations/resolve."""
    return secrets.token_urlsafe(16)
