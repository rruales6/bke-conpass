"""Password recovery by e-mailed code (D21).

POST /auth/forgot-password mints a 6-digit code, stores only "salt$sha256(salt+code)" and mails
it; POST /auth/reset-password checks it and sets the new password.

Anti-enumeration: a request for an unknown address (or the demo owner) leaves a SPENT
placeholder row (user_id = NIL_USER, used_at set) so the per-address rate limit counts it and
the answer is the same 202 either way.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from .demo import DEMO_OWNER_EMAIL
from .email_reset import reset_code_email
from .errors import RateLimited, ValidationFailed
from .logging import get_logger

log = get_logger(__name__)

CODE_TTL_MINUTES = 15
MAX_REQUESTS = 3              # per address per CODE_TTL_MINUTES window
MAX_ATTEMPTS = 5              # wrong tries before the code burns
MIN_PASSWORD, MAX_PASSWORD = 8, 72
NIL_USER = "00000000-0000-0000-0000-000000000000"
_TABLE = "password_reset_codes"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_code(code: str, salt: str) -> str:
    return f"{salt}${hashlib.sha256((salt + code).encode()).hexdigest()}"


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_matches(stored: str, code: str) -> bool:
    """Constant-time check of `code` against a stored "salt$hash"."""
    salt, _, _digest = stored.partition("$")
    return hmac.compare_digest(hash_code(code, salt).encode(), stored.encode())


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _find_user_id(client, email: str) -> str | None:
    rows = client.table("profiles").select("user_id,email").eq("email", email).execute().data
    return rows[0]["user_id"] if rows else None


def _unused(client, email: str) -> list[dict]:
    rows = client.table(_TABLE).select("*").eq("email", email).execute().data or []
    return [r for r in rows if not r.get("used_at")]


def _burn(client, row_id: str, now: datetime) -> None:
    client.table(_TABLE).update({"used_at": now.isoformat()}).eq("id", row_id).execute()


def request_code(client, email: str, language: str | None = None, *,
                 now: datetime | None = None, sender=None) -> None:
    """Handle a forgot-password request. Returns None whatever the address is; raises
    RateLimited (too_many_requests) past MAX_REQUESTS in the window."""
    now = now or datetime.now(UTC)
    email = normalize_email(email)
    window = (now - timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
    recent = (client.table(_TABLE).select("id", count="exact").eq("email", email)
              .gte("created_at", window).execute())
    if (recent.count if recent.count is not None else len(recent.data or [])) >= MAX_REQUESTS:
        raise RateLimited("too many requests for this address, try again later",
                          details={"reason": "too_many_requests"})

    user_id = None if email == DEMO_OWNER_EMAIL.lower() else _find_user_id(client, email)
    if user_id is None:
        client.table(_TABLE).insert({
            "email": email, "user_id": NIL_USER, "code_hash": "none",
            "expires_at": now.isoformat(), "used_at": now.isoformat(),
            "created_at": now.isoformat()}).execute()
        return

    for row in _unused(client, email):       # the previous codes stop working
        _burn(client, row["id"], now)
    code = new_code()
    client.table(_TABLE).insert({
        "email": email, "user_id": user_id,
        "code_hash": hash_code(code, secrets.token_hex(8)),
        "expires_at": (now + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
        "created_at": now.isoformat()}).execute()
    try:
        if sender is None:
            from .providers import get_email_sender
            sender = get_email_sender()
        sender.send(email, *reset_code_email(language, code=code, minutes=CODE_TTL_MINUTES))
    except Exception:  # noqa: BLE001 - the answer must not reveal that the account exists
        log.warning("reset_code_email_failed", exc_info=True)


def _reason(message: str, reason: str) -> ValidationFailed:
    return ValidationFailed(message, details={"reason": reason})


def reset_with_code(client, email: str, code: str, new_password: str, *,
                    now: datetime | None = None, complete=None) -> None:
    """Check the code and set the new password. 422 details.reason: weak_password,
    expired_code, invalid_code, too_many_attempts, same_password."""
    now = now or datetime.now(UTC)
    email = normalize_email(email)
    if not (MIN_PASSWORD <= len(new_password) <= MAX_PASSWORD):
        raise _reason("password must be 8 to 72 characters", "weak_password")
    live = sorted((r for r in _unused(client, email) if _ts(r["expires_at"]) > now),
                  key=lambda r: _ts(r["created_at"]), reverse=True)
    if not live:
        raise _reason("the code expired or was already used", "expired_code")
    row = live[0]                              # only the latest code is valid
    well_formed = len(code) == 6 and code.isascii() and code.isdigit()
    if not well_formed:
        raise _reason("that code is not right", "invalid_code")   # costs no attempt
    if not code_matches(row["code_hash"], code):
        attempts = int(row.get("attempts") or 0) + 1
        patch: dict = {"attempts": attempts}
        if attempts >= MAX_ATTEMPTS:
            patch["used_at"] = now.isoformat()                    # the code burns
        client.table(_TABLE).update(patch).eq("id", row["id"]).execute()
        if attempts >= MAX_ATTEMPTS:
            raise _reason("too many wrong codes, ask for a new one", "too_many_attempts")
        raise _reason("that code is not right", "invalid_code")

    if complete is None:
        from .provisioning import complete_password_change as complete
    complete(row["user_id"], new_password, email=email)   # may raise same_password / weak
    for r in _unused(client, email):
        _burn(client, r["id"], now)
