#!/usr/bin/env python3
"""Run a command with conpass secrets exported as the env vars serverless.yml expects.

Usage:  python scripts/with_env.py npx serverless deploy --stage prod

Reads conpass.* from secrets.yaml (via conpass_common.config) so the Supabase/Google
values never live in shell history or a committed .env. AWS credentials are NOT injected
here — they come from your `aws` CLI login (profile/SSO) or the environment.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "layers/common/python")
from conpass_common.config import settings  # noqa: E402


def _sa_json_min() -> str | None:
    """Resolve the service-account key to *minified* JSON content.

    The Lambda receives the key inline via this env var (a bare path would not
    resolve at runtime). Minifying keeps all env vars under AWS Lambda's 4 KB total.
    """
    content = settings.google_wallet_sa_content
    if not content:
        return None
    return json.dumps(json.loads(content), separators=(",", ":"))


ENV = {
    "SUPABASE_URL": settings.supabase_url,
    "SUPABASE_PUBLISHABLE_KEY": settings.supabase_publishable_key,
    "SUPABASE_SECRET_KEY": settings.supabase_secret_key,
    "SUPABASE_JWKS_URL": settings.supabase_jwks_url,
    "GOOGLE_WALLET_ISSUER_ID": settings.google_wallet_issuer_id,
    "GOOGLE_WALLET_SA_JSON": _sa_json_min(),
    "AWS_REGION": settings.aws_region,
    "CONPASS_CORS_ORIGINS": ",".join(settings.cors_origins),
}


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python scripts/with_env.py <command> [args...]")
    env = dict(os.environ)
    for key, val in ENV.items():
        if val:
            env[key] = str(val)
    os.execvpe(sys.argv[1], sys.argv[1:], env)


if __name__ == "__main__":
    main()
