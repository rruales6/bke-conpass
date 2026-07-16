"""Supabase JWT verification (asymmetric, JWKS) and the request Identity.

Supabase now signs access tokens with asymmetric keys (ES256/RS256). We verify locally
against the project's JWKS endpoint (cached), so no Auth-server round-trip and no shared
HS256 secret. See settings.supabase_jwks_url.

Offline/local: if no jwks_url is configured we DECODE without verifying so the app is
runnable, and log a loud warning. Never rely on that with a real token in production.

App roles (platform_admin / merchant_owner / operation_user / customer) live in the
token's `app_metadata` (the top-level `role` claim is the Postgres role, e.g.
`authenticated`, and is not used for app authorization).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .config import settings
from .errors import Forbidden, Unauthorized
from .logging import get_logger

log = get_logger(__name__)

# Supabase asymmetric signing algorithms.
ALGORITHMS = ["ES256", "RS256"]


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str | None
    roles: list[str]
    merchant_id: str | None = None
    station: str | None = None

    def require_role(self, *roles: str) -> None:
        if not any(r in self.roles for r in roles):
            raise Forbidden(f"requires one of roles: {', '.join(roles)}")

    def require_merchant(self, merchant_id: str) -> None:
        if "platform_admin" in self.roles:
            return
        if self.merchant_id != merchant_id:
            raise Forbidden("cross-tenant access denied")


@lru_cache(maxsize=1)
def _jwk_client():
    from jwt import PyJWKClient  # PyJWT[crypto], in the layer

    return PyJWKClient(settings.supabase_jwks_url, cache_keys=True)


def _claims_to_identity(claims: dict) -> Identity:
    meta = claims.get("app_metadata", {}) or {}
    roles = meta.get("roles") or ([meta["role"]] if meta.get("role") else [])
    return Identity(
        user_id=claims.get("sub", ""),
        email=claims.get("email"),
        roles=list(roles),
        merchant_id=meta.get("merchant_id"),
        station=meta.get("station"),
    )


def identity_from_token(token: str) -> Identity:
    import jwt  # PyJWT

    try:
        if settings.supabase_jwks_url:
            signing_key = _jwk_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, algorithms=ALGORITHMS,
                                audience="authenticated")
        else:  # offline fallback — do NOT rely on this in production
            log.warning("jwt_unverified_decode: no supabase_jwks_url configured")
            claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise Unauthorized(f"invalid token: {exc}") from exc

    ident = _claims_to_identity(claims)
    if not ident.user_id:
        raise Unauthorized("token missing subject")
    return ident


def identity_from_header(authorization: str | None) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token")
    return identity_from_token(authorization.split(" ", 1)[1].strip())
