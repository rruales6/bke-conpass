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
    ref = u.hostname.split(".")[1] if u.hostname.startswith("db.") else u.hostname.split(".")[0]
    pwd = unquote(u.password or "")

    # 1) Try the configured host directly (works when IPv6/direct is reachable).
    try:
        return psycopg.connect(host=u.hostname, port=u.port or 5432, dbname="postgres",
                               user=unquote(u.username or "postgres"), password=pwd,
                               sslmode="require", connect_timeout=8)
    except Exception as exc:  # noqa: BLE001
        print(f"direct connect failed ({exc}); trying IPv4 session pooler…")

    # 2) Auto-detect the pooler region.
    for reg in REGIONS:
        host = f"aws-0-{reg}.pooler.supabase.com"
        try:
            conn = psycopg.connect(host=host, port=5432, dbname="postgres",
                                   user=f"postgres.{ref}", password=pwd,
                                   sslmode="require", connect_timeout=8)
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
