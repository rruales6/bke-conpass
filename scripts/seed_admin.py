#!/usr/bin/env python3
"""Idempotently seed a platform_admin login for testing the /admin/* endpoints.

A platform_admin is cross-tenant (no merchant_id) and powerful — these are TEST
credentials, not public (unlike the /demo sandbox). Rotate the password for production.
Keys on the fixed email, so re-running just re-asserts the password/role.

Usage:  python scripts/seed_admin.py     (needs Supabase secrets)
"""
from __future__ import annotations

import sys

sys.path.insert(0, "layers/common/python")

from conpass_common.db import service_client  # noqa: E402
from conpass_common.provisioning import provision_user  # noqa: E402

ADMIN_EMAIL = "admin@conpass.cards"
ADMIN_PASSWORD = "Conpass-Admin-2026!"  # test credential — rotate for production


def main() -> None:
    c = service_client()
    rows = (c.table("profiles").select("*").eq("email", ADMIN_EMAIL)
            .eq("role", "platform_admin").limit(1).execute().data)
    if rows:
        c.auth.admin.update_user_by_id(rows[0]["user_id"], {"password": ADMIN_PASSWORD})
        print(f"↻ reused platform_admin {ADMIN_EMAIL}")
    else:
        admin = provision_user(email=ADMIN_EMAIL, roles=["platform_admin"], name="Platform Admin")
        c.auth.admin.update_user_by_id(admin.user_id, {"password": ADMIN_PASSWORD})
        print(f"＋ created platform_admin {ADMIN_EMAIL}")
    print(f"\nadmin ready → {ADMIN_EMAIL}  password={ADMIN_PASSWORD}")


if __name__ == "__main__":
    main()
