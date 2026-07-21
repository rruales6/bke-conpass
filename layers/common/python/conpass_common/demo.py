"""Public self-serve demo sandbox constants (Phase 5).

One canonical demo tenant, seeded idempotently by scripts/seed_demo.py and discovered at
runtime by GET /demo. The password is a *shared, intentionally-public* sandbox credential:
the demo login only ever sees demo-flagged data (tenant RLS + merchant_id scoping), so
exposing it is safe — and is the point (visitors get instant, frictionless access).
"""
from __future__ import annotations

DEMO_OWNER_EMAIL = "demo-owner@conpass.cards"
DEMO_PASSWORD = "conpass-demo-2026"          # shared sandbox password (public by design)
DEMO_BUSINESS_NAME = "Café Vecino (Demo)"
DEMO_PROGRAM_NAME = "Club Café Vecino"
DEMO_REWARD = "La 8.ª bebida es gratis"
DEMO_STAMPS_FOR_REWARD = 8
DEMO_COLOR = "#2F6B4F"
