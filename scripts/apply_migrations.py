#!/usr/bin/env python3
"""Apply db/migrations/*.sql in order to the Supabase Postgres.

New Supabase projects expose the DIRECT connection (db.<ref>.supabase.co) over IPv6
only. Where IPv6 isn't available, use the IPv4 session pooler (port 5432 — supports DDL;
transaction mode on 6543 does not). This script prefers conpass.supabase.db_pooler_url
if present, else falls back to the direct db_url, else auto-detects the pooler region.

Usage:  python scripts/apply_migrations.py [--dry-run]
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, "layers/common/python")
import psycopg  # noqa: E402
from conpass_common.config import settings  # noqa: E402

REGIONS = ["us-east-1", "us-east-2", "us-west-1", "sa-east-1", "eu-central-1",
           "ap-southeast-1", "eu-west-2", "ca-central-1", "ap-south-1"]


def _connect() -> psycopg.Connection:
    url = settings.supabase_db_url
    if not url:
        sys.exit("conpass.supabase.db_url not configured")
    u = urlparse(url)
    user = unquote(u.username or "postgres")
    pwd = unquote(u.password or "")
    # The pooler user is `postgres.<ref>`; a direct host is db.<ref>.supabase.co.
    if user.startswith("postgres."):
        ref = user.split(".", 1)[1]
    elif (u.hostname or "").startswith("db."):
        ref = u.hostname.split(".")[1]
    else:
        ref = (u.hostname or "").split(".")[0]
    # Migrations run DDL, which needs SESSION mode. The db_url uses the 6543 transaction
    # pooler (for the app's runtime pooling) — force 5432 (session) here. autocommit=True is
    # ESSENTIAL: without it the tracker SELECT leaves a txn open, turning each
    # `with conn.transaction()` into a savepoint that never top-level commits, so close()
    # silently rolls everything back (migrations printed "applied" but never persisted).
    port = 5432 if (u.port in (None, 6543)) else u.port

    # 1) Try the configured host directly (works when IPv6/direct is reachable).
    try:
        return psycopg.connect(host=u.hostname, port=port, dbname="postgres",
                               user=user, password=pwd, sslmode="require",
                               connect_timeout=8, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        print(f"direct connect failed ({exc}); trying IPv4 session pooler…")

    # 2) Auto-detect the pooler region (session mode, port 5432).
    for reg in REGIONS:
        host = f"aws-0-{reg}.pooler.supabase.com"
        try:
            conn = psycopg.connect(host=host, port=5432, dbname="postgres",
                                   user=f"postgres.{ref}", password=pwd,
                                   sslmode="require", connect_timeout=8, autocommit=True)
            print(f"connected via session pooler ({reg})")
            return conn
        except Exception:  # noqa: BLE001, S112
            continue
    sys.exit("could not connect via direct host or any pooler region")


TRACKER = "conpass_schema_migrations"


def _ensure_tracker(conn: psycopg.Connection) -> set[str]:
    conn.execute(
        f"create table if not exists {TRACKER} "
        "(filename text primary key, applied_at timestamptz not null default now())"
    )
    conn.execute(f"revoke all on {TRACKER} from anon, authenticated")
    conn.commit()
    return {r[0] for r in conn.execute(f"select filename from {TRACKER}").fetchall()}


def main() -> None:
    dry = "--dry-run" in sys.argv
    baseline = "--baseline" in sys.argv  # mark existing files applied WITHOUT running them
    conn = _connect()
    applied = _ensure_tracker(conn)
    for path in sorted(glob.glob("db/migrations/*.sql")):
        name = Path(path).name
        if name in applied:
            print("skip (already applied)", name)
            continue
        if dry:
            print("would apply", name)
            continue
        with conn.transaction():
            if not baseline:
                conn.execute(Path(path).read_text())
            conn.execute(f"insert into {TRACKER}(filename) values (%s)", (name,))
        print(("baselined" if baseline else "applied"), name)
    conn.close()
    print("done.")


if __name__ == "__main__":
    main()
