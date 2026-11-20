"""Phase 18 — ProgramMetrics additions (secondVisitNew/Returned, redemptions30d,
levelDistribution) and the deprecated `inactivityDays` (ignored on write, always 90)."""
from __future__ import annotations

from types import SimpleNamespace

from conpass_common.app import require_identity
from conpass_common.auth import Identity
from fastapi.testclient import TestClient

from services.programs import handler as h

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
P1 = "33333333-3333-3333-3333-333333333333"

VIEW = {"program_id": P1, "visits": 9, "redemptions": 2, "active_installed_passes": 7,
        "installs_this_week": 1, "eligible_for_reminder": 3, "second_visit_rate_30d": 37.5,
        "churn_rate": 10.0, "inactivity_days": 30}


class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return SimpleNamespace(data=self._data)


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _Client:
    def __init__(self, rpcs=None, view=(VIEW,)):
        self.rpcs, self.calls, self.view = rpcs, [], list(view)

    def table(self, name):
        assert name == "program_metrics_view"
        return _Table(self.view)

    def rpc(self, name, params):
        self.calls.append((name, params))
        if self.rpcs is None:
            raise RuntimeError("function report_second_visit does not exist")
        return _Exec(self.rpcs[name])


RPCS = {
    "report_second_visit": [{"new_customers": 8, "returned": 3}],
    "report_activity": [{"visits": 12, "redemptions": 4}],
    "report_levels": [{"level": 1, "count": 5}, {"level": 3, "count": 2}],
}


def _program(**over):
    return {"id": P1, "merchant_id": M1, "type": "loyalty_stamps", "name": "Café",
            "created_at": "2026-10-20T00:00:00Z", **over}


def _get(monkeypatch, client, program):
    monkeypatch.setattr(h, "get_repo", lambda: SimpleNamespace(get=lambda pid: program))
    monkeypatch.setattr(h, "service_client", lambda: client)
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], merchant_id=M1))
    return TestClient(h.app).get(f"/programs/{P1}/metrics")


def test_metrics_carry_console_fields(monkeypatch):
    client = _Client(RPCS)
    r = _get(monkeypatch, client, _program())
    assert r.status_code == 200, r.text
    body = r.json()
    # Existing fields are untouched...
    assert body["visits"] == 9 and body["secondVisitRate30d"] == 37.5
    assert body["churnRate"] == 10.0
    # ...and the new ones are added.
    assert body["secondVisitNew"] == 8 and body["secondVisitReturned"] == 3
    assert body["redemptions30d"] == 4
    assert body["levelDistribution"] == [{"level": 1, "count": 5}, {"level": 3, "count": 2}]
    names = [n for n, _ in client.calls]
    assert names == ["report_second_visit", "report_activity", "report_levels"]
    for _, params in client.calls:
        assert params["p_program"] == P1 and params["p_merchant"] == M1
        assert params["p_locations"] is None


def test_metrics_inactivity_days_is_always_90(monkeypatch):
    body = _get(monkeypatch, _Client(RPCS), _program(inactivity_days=30)).json()
    assert body["inactivityDays"] == 90


def test_metrics_for_seasonal_have_no_level_distribution(monkeypatch):
    client = _Client(RPCS, view=[{**VIEW, "churn_rate": None, "inactivity_days": None}])
    body = _get(monkeypatch, client, _program(type="seasonal_discount")).json()
    assert "levelDistribution" not in body and "inactivityDays" not in body
    assert body["secondVisitNew"] == 8
    assert "report_levels" not in [n for n, _ in client.calls]


def test_metrics_zero_fallback_still_carries_console_fields(monkeypatch):
    client = _Client({"report_second_visit": [], "report_activity": [], "report_levels": []},
                     view=[])
    body = _get(monkeypatch, client, _program()).json()
    assert body["visits"] == 0 and body["inactivityDays"] == 90
    assert body["secondVisitNew"] == 0 and body["secondVisitReturned"] == 0
    assert body["redemptions30d"] == 0 and body["levelDistribution"] == []


def test_metrics_survive_missing_sql_functions(monkeypatch):
    """Before migration 0018 is applied the RPCs fail: the old payload is returned."""
    body = _get(monkeypatch, _Client(None), _program()).json()
    assert body["visits"] == 9 and body["inactivityDays"] == 90
    for key in ("secondVisitNew", "secondVisitReturned", "redemptions30d",
                "levelDistribution"):
        assert key not in body


class _Repo:
    def __init__(self, row=None):
        self.created, self.row = [], row

    def is_demo(self, _m):
        return False

    def count_live_visitor_campaigns(self, _m):
        return 0

    def create(self, data):
        self.created.append(data)
        return {"id": P1, "created_at": "2026-11-19T00:00:00Z", **data}

    def get(self, _pid):
        return self.row

    def update(self, _pid, patch):
        self.row = {**self.row, **patch}
        return self.row


def _owner(monkeypatch):
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], merchant_id=M1))


def test_post_ignores_inactivity_days_and_reports_90(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _owner(monkeypatch)
    r = TestClient(h.app).post("/programs", json={
        "type": "loyalty_stamps", "name": "Club", "mechanic": "stamps",
        "stampsForReward": 8, "reward": "Café gratis", "wallets": ["google"], "appearance": {},
        "inactivityDays": 30})
    assert r.status_code == 201, r.text
    assert repo.created[0]["inactivity_days"] is None
    assert r.json()["inactivityDays"] == 90


def test_patch_ignores_inactivity_days(monkeypatch):
    repo = _Repo(_program(inactivity_days=45))
    monkeypatch.setattr(h, "get_repo", lambda: repo)
    _owner(monkeypatch)
    r = TestClient(h.app).patch(f"/programs/{P1}", json={"inactivityDays": 10, "name": "Nuevo"})
    assert r.status_code == 200, r.text
    assert repo.row["inactivity_days"] == 45          # untouched
    assert r.json()["inactivityDays"] == 90
