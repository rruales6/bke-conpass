"""End-to-end integration against the LIVE Supabase Data API.

Exercises the core loop: onboard merchant → create program → enroll (issue card) →
accrue → redeem, verifying backend-authoritative balances, then deletes the merchant
(cascade cleans everything). Skipped automatically when no Supabase secret is configured
(so CI stays offline and green).
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.config import settings
from conpass_common.db import service_client
from conpass_common.idempotency import InMemoryIdempotencyStore
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not settings.supabase_secret_key, reason="no Supabase secret configured")


def _owner(merchant_id: str) -> Identity:
    return Identity(user_id=str(uuid.uuid4()), email="owner@test.co",
                    roles=["merchant_owner"], merchant_id=merchant_id)


def test_full_loop():
    c = service_client()
    merchant_id = None
    card_id = None
    wallet_on = bool(settings.google_wallet_issuer_id and settings.google_wallet_sa_content)
    try:
        # 1) Onboard merchant (public). Unique email so a prior failed run can't 409.
        from services.merchants.handler import app as merchants_app
        owner_email = f"it-{uuid.uuid4().hex[:8]}@conpasstest.io"
        r = TestClient(merchants_app).post("/merchants", json={
            "businessName": "__conpass_it", "ruc": "0999999999001",
            "category": "cafe_restaurant", "city": "Quito",
            "contactName": "IT", "contactEmail": owner_email,
            "tier": "growth",
            "payment": {"method": "manual_transfer",
                        "proofStorageKey": "payment-proofs/it-test.pdf"},
        })
        assert r.status_code == 201, r.text
        merchant_id = r.json()["merchant"]["id"]
        assert r.json()["subscription"]["programLimit"] == 3  # growth tier default
        assert r.json()["ownerInviteSent"] and r.json()["ownerTempPassword"]  # Phase 2

        owner = _owner(merchant_id)

        # 2) Create a stamps program (owner)
        from services.programs.handler import app as programs_app
        programs_app.dependency_overrides[require_identity] = lambda: owner
        r = TestClient(programs_app).post("/programs", json={
            "type": "loyalty_stamps", "name": "Club Café", "mechanic": "stamps",
            "stampsForReward": 8, "reward": "8th coffee free",
            "welcomeBonus": 1, "appearance": {"color": "#1E7A50"}, "wallets": ["google"],
        })
        assert r.status_code == 201, r.text
        program = r.json()
        program_id = program["id"]
        assert program["enrollmentUrl"].endswith(program_id)
        programs_app.dependency_overrides.clear()

        # 3) Enroll a customer (public) → card issued with welcome bonus (1 stamp)
        from services.enrollment import handler as enroll_h
        enroll_h.get_idempotency_store = lambda: InMemoryIdempotencyStore()
        r = TestClient(enroll_h.app).post(
            f"/programs/{program_id}/enroll",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"fullName": "María Torres", "consent": True, "dedupeKey": "dev-123"})
        assert r.status_code == 201, r.text
        card = r.json()["card"]
        card_id = card["id"]
        assert card["balance"]["stamps"] == 1          # welcome bonus
        assert card["holderName"] == "María T."
        if wallet_on:  # Phase 4: enroll issued a real Google Wallet object + save link
            link = r.json()["walletLinks"].get("google")
            assert link and link.startswith("https://pay.google.com/gp/v/save/"), \
                r.json()["walletLinks"]

        # 3b) Re-enroll same device → same card, 200 (dedupe)
        r2 = TestClient(enroll_h.app).post(
            f"/programs/{program_id}/enroll",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"fullName": "María Torres", "dedupeKey": "dev-123"})
        assert r2.status_code == 200
        assert r2.json()["card"]["id"] == card_id

        # 4) Cashier accrues 3 stamps → 1+3=4 (backend-authoritative)
        op_user_id = str(uuid.uuid4())
        c.table("profiles").insert({"user_id": op_user_id, "merchant_id": merchant_id,
                                    "role": "operation_user", "station": "caja_1"}).execute()
        from services.operations import handler as ops_h
        ops_h.get_idempotency_store = lambda: InMemoryIdempotencyStore()
        ops_h.app.dependency_overrides[require_identity] = lambda: Identity(
            user_id=op_user_id, email="op@test.co", roles=["operation_user"],
            merchant_id=merchant_id)
        key = str(uuid.uuid4())
        payload = {"cardId": card_id, "operationUserId": op_user_id, "stamps": 3}
        a1 = TestClient(ops_h.app).post("/operations/accrue", json=payload,
                                        headers={"Idempotency-Key": key})
        assert a1.status_code == 200, a1.text
        assert a1.json()["card"]["balance"]["stamps"] == 4

        # 4b) DB reflects the authoritative balance
        db_card = c.table("cards").select("stamps").eq("id", card_id).execute().data[0]
        assert db_card["stamps"] == 4

        # 5) Accrue to threshold → reward earned, rollover
        TestClient(ops_h.app).post(
            "/operations/accrue",
            json={"cardId": card_id, "operationUserId": op_user_id, "stamps": 4},
            headers={"Idempotency-Key": str(uuid.uuid4())})
        db_card = c.table("cards").select("*").eq("id", card_id).execute().data[0]
        assert db_card["rewards_available"] == 1 and db_card["stamps"] == 0

        # 6) Redeem the reward
        red = TestClient(ops_h.app).post(
            "/operations/redeem",
            json={"cardId": card_id, "operationUserId": op_user_id},
            headers={"Idempotency-Key": str(uuid.uuid4())})
        assert red.status_code == 200
        assert red.json()["card"]["balance"]["rewardsAvailable"] == 0
        ops_h.app.dependency_overrides.clear()
    finally:
        # Deactivate the Google Wallet object we created (passes can't be deleted).
        if wallet_on and card_id:
            with contextlib.suppress(Exception):
                from conpass_common.providers import get_wallet_provider
                oid = f"{settings.google_wallet_issuer_id}.card_{card_id}"
                get_wallet_provider().revoke(oid)
        if merchant_id:
            # Onboarding provisions a Supabase Auth owner (no FK to auth.users), so grab
            # the profile user_ids before the merchant cascade removes the profiles.
            uids = [p["user_id"] for p in c.table("profiles").select("user_id")
                    .eq("merchant_id", merchant_id).execute().data]
            c.table("merchants").delete().eq("id", merchant_id).execute()
            for uid in uids:
                # some profiles (e.g. the seeded operator) aren't real auth users
                with contextlib.suppress(Exception):
                    c.auth.admin.delete_user(uid)
