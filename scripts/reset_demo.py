#!/usr/bin/env python3
"""Wipe demo *activity* (cards, transactions, customers) for every demo merchant, while
keeping the merchant, program and owner login intact (and re-creating the demo
sample customer + card). Idempotent — run any time the
sandbox gets messy (a scheduled nightly reset can just invoke this).

Usage:  python scripts/reset_demo.py     (needs Supabase secrets)
"""
from __future__ import annotations

import sys

sys.path.insert(0, "layers/common/python")

from conpass_common.db import service_client  # noqa: E402
from conpass_common.demo import ensure_sample_card  # noqa: E402


def main() -> None:
    c = service_client()
    demos = c.table("merchants").select("id,business_name").eq("is_demo", True).execute().data
    if not demos:
        print("no demo merchants — nothing to reset")
        return
    for m in demos:
        mid = m["id"]
        # transactions reference cards; delete activity oldest-dependency first.
        c.table("transactions").delete().eq("merchant_id", mid).execute()
        c.table("cards").delete().eq("merchant_id", mid).execute()
        c.table("customers").delete().eq("merchant_id", mid).execute()
        # Re-create the sample customer/card so the demo cashier prefill keeps working.
        ensure_sample_card(c, mid)
        print(f"↺ reset {m['business_name']} ({mid})")


if __name__ == "__main__":
    main()
