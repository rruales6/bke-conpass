"""Configuration loader.

Precedence: process env vars > secrets.yaml > default.
secrets.yaml lives outside the repo (never committed). Secrets are nested under a
top-level `conpass:` key. Path is overridable via CONPASS_SECRETS_FILE for CI.

Uses Supabase's CURRENT key model:
  * publishable_key (sb_publishable_…) → anon/authenticated roles (browser-safe, RLS-gated)
  * secret_key      (sb_secret_…)      → service_role (backend only, BYPASSES RLS)
  * jwks_url        → asymmetric JWT verification (ES256/RS256); no shared HS256 secret
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

DEFAULT_SECRETS_FILE = "/Users/jruales/secrets/secrets.yaml"
ROOT = "conpass"  # secrets are nested under this key


@lru_cache(maxsize=1)
def _secrets() -> dict[str, Any]:
    path = Path(os.environ.get("CONPASS_SECRETS_FILE", DEFAULT_SECRETS_FILE))
    if yaml is None or not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _get(*keys: str, env: str | None = None, default: Any = None) -> Any:
    """Env var first, then conpass.<...> path in secrets.yaml."""
    if env and (val := os.environ.get(env)) is not None:
        return val
    node: Any = _secrets().get(ROOT, {})
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


class Settings:
    """Lazily-resolved settings. Missing values stay None until a feature needs them,
    so the codebase runs offline until it touches a credentialed integration."""

    # --- Supabase ---
    @property
    def supabase_url(self) -> str | None:
        return _get("supabase", "url", env="SUPABASE_URL")

    @property
    def supabase_publishable_key(self) -> str | None:
        return _get("supabase", "publishable_key", env="SUPABASE_PUBLISHABLE_KEY")

    @property
    def supabase_secret_key(self) -> str | None:
        """Backend-only key (service_role, bypasses RLS)."""
        return _get("supabase", "secret_key", env="SUPABASE_SECRET_KEY")

    @property
    def supabase_jwks_url(self) -> str | None:
        return _get("supabase", "jwks_url", env="SUPABASE_JWKS_URL")

    @property
    def supabase_db_url(self) -> str | None:
        return _get("supabase", "db_url", env="SUPABASE_DB_URL")

    # --- AWS ---
    @property
    def aws_region(self) -> str:
        return _get("aws", "region", env="AWS_REGION", default="us-east-1")

    # --- Google Wallet ---
    @property
    def google_wallet_issuer_id(self) -> str | None:
        return _get("google_wallet", "issuer_id", env="GOOGLE_WALLET_ISSUER_ID")

    @property
    def google_wallet_service_account_json(self) -> str | None:
        return _get("google_wallet", "service_account_json", env="GOOGLE_WALLET_SA_JSON")

    # --- Provider selection (stubbed by default per D9/D10) ---
    @property
    def payment_provider(self) -> str:
        return os.environ.get("CONPASS_PAYMENT_PROVIDER", "manual")

    @property
    def notification_provider(self) -> str:
        return os.environ.get("CONPASS_NOTIFICATION_PROVIDER", "stub")

    @property
    def wallet_provider(self) -> str:
        return os.environ.get("CONPASS_WALLET_PROVIDER", "google")

    # --- Frontend / CORS ---
    @property
    def cors_origins(self) -> list[str]:
        raw = os.environ.get("CONPASS_CORS_ORIGINS")
        if raw:
            return [o.strip() for o in raw.split(",") if o.strip()]
        # Production frontend is https://conpass.cards; allow local dev too.
        return ["https://conpass.cards", "http://localhost:5173", "http://localhost:3000"]

    @property
    def stage(self) -> str:
        return os.environ.get("CONPASS_STAGE", "local")


settings = Settings()
