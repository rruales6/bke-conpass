# bke-conpass — conpass backend

AWS Lambda (Python 3.12, ARM64) backend for the conpass loyalty platform. One
single-responsibility FastAPI + Mangum Lambda per feature, behind an HTTP API, sharing a
Supabase Postgres database. Contract-first (OpenAPI 3.1).

- **Contracts:** [`contracts/openapi.yaml`](contracts/openapi.yaml) — single source of truth.
- **Shared layer:** [`layers/common/python/conpass_common`](layers/common/python/conpass_common)
  (config, Supabase client, JWKS auth, idempotency, wallet/payment/notification providers).
- **Services:** [`services/*`](services) — one Lambda per feature.
- **DB:** [`db/migrations`](db/migrations) — schema + RLS + Data-API safety grants.
- **Infra:** [`serverless.yml`](serverless.yml) (Serverless Framework v3).

Sibling repos: **fte-conpass** (frontend PWA), **docs-conpass** (architecture, decisions,
AWS setup, design reference).

## Live
Prod API: `https://c8glyvxjh7.execute-api.us-east-1.amazonaws.com` (us-east-1).

## Develop
```bash
make install          # venv + deps
make check            # ruff + pytest (17 tests; live integration skipped without secrets)
make db-apply         # apply migrations (needs Supabase creds)
```
Secrets live outside the repo in `~/secrets/secrets.yaml` under `conpass.*`
(see [`.env.example`](.env.example)). Deploy: `scripts/deploy.sh conpass prod`.
