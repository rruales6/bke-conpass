#!/usr/bin/env bash
# Deploy the backend. Handles AWS SSO / `login_session` profiles, which the Serverless
# Framework's AWS SDK v2 can't resolve on its own: we export the resolved temporary
# credentials as env vars, then deploy. Supabase/Google secrets are injected by with_env.py.
#
# Usage:  scripts/deploy.sh [profile=conpass] [stage=prod] [extra serverless args...]
# Anything after the stage is forwarded to `serverless deploy` — notably `--force`, which
# you need whenever the change is serverless.yml-only (serverless otherwise no-ops it).
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="${1:-conpass}"
STAGE="${2:-prod}"
shift $(( $# < 2 ? $# : 2 ))   # the rest passes through to serverless
export PATH="/usr/local/bin:$PATH"   # aws cli location

if ! command -v aws >/dev/null; then
  echo "aws CLI not found on PATH" >&2; exit 1
fi

# Resolve temp credentials from the (possibly SSO/login_session) profile.
eval "$(aws configure export-credentials --profile "$PROFILE" --format env)"
unset AWS_PROFILE   # force the SDK to use the exported env credentials

exec .venv/bin/python scripts/with_env.py npx serverless deploy --stage "$STAGE" "$@"
