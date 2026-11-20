"""Phase 18 Wave-1 review fixes (Opus).

1. Transactions record the authenticated caller, never the client-sent `operationUserId`.
2. The birthday endpoint also accepts the signed pass-back link token (card_links, D15).
"""
from __future__ import annotations

from types import SimpleNamespace

from conpass_common import card_links

from tests.test_phase18_enrollment import CARD1, CUST1, _bday, _put
from tests.test_phase18_enrollment import repo as repo  # noqa: F401 - fixture
from tests.test_phase18_operations import OP1, OP2  # noqa: F401 - fixture
from tests.test_phase18_operations import env as env


def test_accrue_records_the_caller_not_the_body(env):  # noqa: F811
    r = env.accrue(who=OP2)                      # caller is OP1, body claims OP2
    assert r.status_code == 200, r.text
    assert env.repo.txns[-1].operation_user_id == OP1
    assert r.json()["transaction"]["operationUserId"] == OP1


def test_redeem_records_the_caller_not_the_body(env):  # noqa: F811
    env.accrue(stamps=1)                          # 7 + 1 = goal → one reward
    r = env.redeem(who=OP2)
    assert r.status_code == 200, r.text
    assert env.repo.txns[-1].operation_user_id == OP1


def test_spoofed_operation_can_still_be_undone_by_its_real_author(env):  # noqa: F811
    txn_id = env.accrue(who=OP2).json()["transaction"]["id"]
    assert env.undo(txn_id).status_code == 200    # OP1 made it, so OP1 may undo it


def _signed(monkeypatch):
    monkeypatch.setattr(card_links, "settings", SimpleNamespace(
        supabase_secret_key="sb_secret_test", public_app_url="https://console.conpass.cards"))
    return card_links.birthday_link_token(CARD1)


def test_birthday_accepts_the_signed_link_token(repo, monkeypatch):  # noqa: F811
    token = _signed(monkeypatch)
    r = _put(repo, **_bday(2, 5, token=token))
    assert r.status_code == 204, r.text
    assert repo.customers[CUST1]["birthday_day"] == 2


def test_birthday_signed_token_is_bound_to_the_card(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(card_links, "settings", SimpleNamespace(
        supabase_secret_key="sb_secret_test", public_app_url="https://x"))
    other = card_links.birthday_link_token("another-card")
    assert _put(repo, **_bday(2, 5, token=other)).status_code == 404


# --- Public enrolment preview (board 09/20) -------------------------------------------------
def test_enroll_preview_is_public_and_carries_the_card(repo, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from services.enrollment import handler as eh
    from tests.test_phase18_enrollment import P1
    monkeypatch.setattr(type(repo), "birthday_offer", lambda self, mid: True, raising=False)
    r = TestClient(eh.app).get(f"/programs/{P1}/enroll")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["businessName"] == "QA" and body["programName"] == "Café"
    assert body["stampsForReward"] == 8 and body["campaignStatus"] == "active"
    assert body["birthdayOffer"] is True
    assert "merchantId" not in body and "opaqueToken" not in body


def test_enroll_preview_hides_membership_passes(repo, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from services.enrollment import handler as eh
    from tests.test_phase18_enrollment import P1
    monkeypatch.setattr(type(repo), "program", dict(repo.program, type="membership_pass"))
    assert TestClient(eh.app).get(f"/programs/{P1}/enroll").status_code == 404
