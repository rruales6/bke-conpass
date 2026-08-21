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

# Resolve temp credentials from the (possibly SSO/login_session) profile. Capture BEFORE
# eval: an expired session makes this print an error and emit nothing on stdout, and
# `eval ""` happily succeeds — which is how a deploy once got as far as creating an empty
# stack in a completely different AWS account.
if ! CREDS="$(aws configure export-credentials --profile "$PROFILE" --format env)"; then
  echo "could not resolve credentials for profile '$PROFILE' — reauthenticate: aws login" >&2
  exit 1
fi
eval "$CREDS"
unset AWS_PROFILE   # force the SDK to use the exported env credentials

# Belt and braces: confirm those credentials really are the conpass account before we let
# CloudFormation touch anything. Override with CONPASS_AWS_ACCOUNT for a different account.
EXPECTED_ACCOUNT="${CONPASS_AWS_ACCOUNT:-154320462594}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [ "$ACCOUNT" != "$EXPECTED_ACCOUNT" ]; then
  echo "refusing to deploy: credentials resolve to AWS account $ACCOUNT, expected $EXPECTED_ACCOUNT" >&2
  echo "(an expired session silently falls back to the ambient/default profile)" >&2
  exit 1
fi

exec .venv/bin/python scripts/with_env.py npx serverless deploy --stage "$STAGE" "$@"
