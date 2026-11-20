"""Phase 18 — reports service (summary / overview / export): who may see what, the plan gates,
the shaping of the SQL results and the CSV format. The counting itself is SQL
(0018_reports.sql, executed live in Wave 3); here a fake client answers each `rpc` and the
tests check the filters the handler sends and the payload it builds."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from fastapi.testclient import TestClient

from services.reports import handler as h
from services.reports import logic
from services.reports.repository import ReportsRepository

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
M2 = "66666666-6666-6666-6666-666666666666"
P1 = "33333333-3333-3333-3333-333333333333"
P2 = "44444444-4444-4444-4444-444444444444"
L1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
L2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
L3 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"

TODAY = date(2026, 11, 19)          # a Thursday; its Monday is 2026-11-16


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _Rpc:
    def __init__(self, data):
        self._data, self._range = data, None

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def execute(self):
        data = self._data
        if self._range:
            data = data[self._range[0]:self._range[1] + 1]
        return SimpleNamespace(data=data)


class FakeClient:
    def __init__(self, tier="pro", staff_role=None, scope=(), results=None):
        self.tables = {
            "subscriptions": [{"merchant_id": M1, "tier": tier}],
            "profiles": [{"user_id": U1, "merchant_id": M1, "staff_role": staff_role}],
            "operation_user_locations": [{"user_id": U1, "location_id": loc} for loc in scope],
        }
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    def table(self, name):
        return _Q(self.tables.get(name, []))

    def rpc(self, name, params):
        self.calls.append((name, params))
        r = self.results.get(name, [])
        return _Rpc(r(params) if callable(r) else r)

    def params(self, name):
        return [p for n, p in self.calls if n == name]


def _setup(monkeypatch, client, roles=("merchant_owner",), merchant_id=M1, today=TODAY):
    monkeypatch.setattr(h, "get_repo", lambda: ReportsRepository(client))
    monkeypatch.setattr(h, "today_ec", lambda: today)
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=list(roles), merchant_id=merchant_id))
    return TestClient(h.app)


RESULTS = {
    "report_second_visit": lambda p: [{"new_customers": 8, "returned": 3}]
    if p["p_to"] == TODAY.isoformat() else [{"new_customers": 10, "returned": 4}],
    "report_activity": [{"visits": 40, "redemptions": 6}],
    "report_program_figures": [{"program_id": P1, "active_cards": 12, "redemptions_30d": 5,
                                "new_cards_7d": 2}],
    "report_cohort_weeks": [
        {"week_start": "2026-11-09", "new_customers": 5, "returned": 2},
        {"week_start": "2026-11-16", "new_customers": 3, "returned": 0},
    ],
    "report_campaigns": [
        {"program_id": P1, "name": "Café", "type": "loyalty_stamps", "visits": 30,
         "redemptions": 4, "new_customers": 8, "returned": 3},
        {"program_id": P2, "name": "Navidad", "type": "seasonal_discount", "visits": 10,
         "redemptions": 2, "new_customers": 4, "returned": 1},
    ],
    "report_levels": [{"level": 1, "count": 9}, {"level": 2, "count": 3}],
    "report_branches": [{"location_id": L1, "name": "Centro", "new_customers": 5, "visits": 20,
                         "returned": 1}],
}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("returned,new,expected", [
    (0, 0, None), (3, 0, None), (0, 5, 0.0), (1, 3, 33.3), (2, 3, 66.7),
    (1, 16, 6.3),        # 6.25 rounds half-up, not to even
    (5, 5, 100.0), (1, 8, 12.5),
])
def test_rate_rounds_half_up_and_is_null_for_empty_cohorts(returned, new, expected):
    assert logic.rate(returned, new) == expected


def test_week_start_is_monday():
    assert logic.week_start(date(2026, 11, 16)) == date(2026, 11, 16)   # Monday
    assert logic.week_start(date(2026, 11, 22)) == date(2026, 11, 16)   # Sunday
    assert logic.week_start(date(2026, 11, 23)) == date(2026, 11, 23)   # next Monday


def test_period_weeks_oldest_first_current_week_included():
    weeks = logic.period_weeks(TODAY, 4)
    assert weeks == [date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9),
                     date(2026, 11, 16)]
    assert len(logic.period_weeks(TODAY, 12)) == 12


def test_period_weeks_on_a_sunday_and_a_monday():
    assert logic.period_weeks(date(2026, 11, 22), 4)[-1] == date(2026, 11, 16)
    assert logic.period_weeks(date(2026, 11, 23), 4)[-1] == date(2026, 11, 23)


def test_cohort_closed_boundary():
    start = date(2026, 10, 5)                        # last day of that week: 10-11; +30 = 11-10
    assert logic.cohort_closed(start, date(2026, 11, 10)) is False
    assert logic.cohort_closed(start, date(2026, 11, 11)) is True
    assert logic.cohort_closed(logic.week_start(TODAY), TODAY) is False


def test_build_cohorts_fills_empty_weeks_and_flags_closed():
    starts = logic.period_weeks(TODAY, 8)
    rows = [{"week_start": "2026-09-28", "new_customers": 4, "returned": 3}]
    out = logic.build_cohorts(rows, starts, TODAY)
    assert [c["weekStart"] for c in out] == [s.isoformat() for s in starts]
    assert out[0] == {"weekStart": "2026-09-28", "newCustomers": 4, "returned": 3,
                      "rate": 75.0, "closed": True}
    assert out[1]["newCustomers"] == 0 and out[1]["rate"] is None
    assert out[-1]["closed"] is False


def test_second_visit_payload_previous_rate_only_when_given():
    assert logic.second_visit(0, 0) == {"newCustomers": 0, "returned": 0, "rate": None}
    sv = logic.second_visit(8, 3, (10, 4))
    assert sv == {"newCustomers": 8, "returned": 3, "rate": 37.5, "previousRate": 40.0}
    assert logic.second_visit(8, 3, (0, 0))["previousRate"] is None


def test_ec_datetime_crosses_midnight():
    dt = logic.ec_datetime("2026-11-19T03:30:00+00:00")
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 11, 18, 22, 30)
    assert logic.ec_datetime("2026-11-19T15:00:00Z").hour == 10
    naive = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)  # noqa: UP017
    assert logic.ec_datetime(naive).day == 18


def test_build_csv_bom_crlf_comma_and_formula_guard():
    raw = logic.build_csv(["A", "B"], [["x,y", 1.5], ["=cmd", None], ["ñ", 2]])
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert text == 'A,B\r\n"x,y",1.5\r\n\'=cmd,\r\nñ,2\r\n'


# --------------------------------------------------------------------------- #
# /reports/summary
# --------------------------------------------------------------------------- #
def test_summary_for_owner(monkeypatch):
    client = FakeClient(results=RESULTS)
    r = _setup(monkeypatch, client).get("/reports/summary")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "secondVisit": {"newCustomers": 8, "returned": 3, "rate": 37.5, "previousRate": 40.0},
        "redemptionsThisMonth": 6,
        "programs": [{"programId": P1, "activeCards": 12, "redemptions30d": 5,
                      "newCards7d": 2}],
    }
    windows = [(p["p_from"], p["p_to"]) for p in client.params("report_second_visit")]
    assert windows == [("2026-10-21", "2026-11-19"), ("2026-09-21", "2026-10-20")]
    month = client.params("report_activity")[0]
    assert (month["p_from"], month["p_to"]) == ("2026-11-01", "2026-11-19")
    assert all(p["p_locations"] is None for _, p in client.calls)


def test_summary_empty_cohort_is_null_rate(monkeypatch):
    client = FakeClient(results={})
    body = _setup(monkeypatch, client).get("/reports/summary").json()
    assert body["secondVisit"] == {"newCustomers": 0, "returned": 0, "rate": None,
                                   "previousRate": None}
    assert body["redemptionsThisMonth"] == 0 and body["programs"] == []


def test_summary_admin_needs_merchant_id(monkeypatch):
    client = FakeClient(results=RESULTS)
    c = _setup(monkeypatch, client, roles=("platform_admin",), merchant_id=None)
    assert c.get("/reports/summary").status_code == 422
    r = c.get(f"/reports/summary?merchantId={M2}")
    assert r.status_code == 200
    assert client.params("report_activity")[0]["p_merchant"] == M2


def test_summary_other_merchant_is_forbidden_for_owner(monkeypatch):
    c = _setup(monkeypatch, FakeClient(results=RESULTS))
    assert c.get(f"/reports/summary?merchantId={M2}").status_code == 403
    assert c.get(f"/reports/summary?merchantId={M1}").status_code == 200


def test_summary_cashier_is_forbidden(monkeypatch):
    client = FakeClient(staff_role="cashier", results=RESULTS)
    c = _setup(monkeypatch, client, roles=("operation_user",))
    assert c.get("/reports/summary").status_code == 403
    assert c.get("/reports/overview").status_code == 403
    assert c.get("/reports/export?section=cohorts").status_code == 403


def test_summary_manager_is_limited_to_branches(monkeypatch):
    client = FakeClient(staff_role="manager", scope=(L1, L2), results=RESULTS)
    c = _setup(monkeypatch, client, roles=("operation_user",))
    assert c.get("/reports/summary").status_code == 200
    assert all(p["p_locations"] == [L1, L2] for _, p in client.calls)


def test_manager_without_pro_is_forbidden(monkeypatch):
    client = FakeClient(tier="growth", staff_role="manager", scope=(L1,), results=RESULTS)
    c = _setup(monkeypatch, client, roles=("operation_user",))
    assert c.get("/reports/summary").status_code == 403


# --------------------------------------------------------------------------- #
# /reports/overview
# --------------------------------------------------------------------------- #
def test_overview_shape_for_pro_owner(monkeypatch):
    client = FakeClient(results=RESULTS)
    r = _setup(monkeypatch, client).get("/reports/overview?weeks=4")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["periodStart"] == "2026-10-26" and body["periodEnd"] == "2026-11-19"
    assert body["weeks"] == 4
    assert body["kpis"] == {
        "secondVisit": {"newCustomers": 8, "returned": 2, "rate": 25.0},
        "newCustomers": 8, "visits": 40, "redemptions": 6}
    assert [c["weekStart"] for c in body["cohorts"]] == [
        "2026-10-26", "2026-11-02", "2026-11-09", "2026-11-16"]
    assert body["cohorts"][2] == {"weekStart": "2026-11-09", "newCustomers": 5, "returned": 2,
                                  "rate": 40.0, "closed": False}
    assert body["cohorts"][3]["rate"] == 0.0
    assert body["campaigns"][0] == {"programId": P1, "name": "Café", "type": "loyalty_stamps",
                                    "visits": 30, "redemptions": 4, "secondVisitRate": 37.5}
    assert body["campaigns"][1]["secondVisitRate"] is None      # discount campaign
    assert body["levels"] == [{"level": 1, "count": 9}, {"level": 2, "count": 3}]
    assert body["branches"] == [{"locationId": L1, "name": "Centro", "newCustomers": 5,
                                 "visits": 20, "secondVisitRate": 20.0}]
    weeks_call = client.params("report_cohort_weeks")[0]
    assert (weeks_call["p_from"], weeks_call["p_to"]) == ("2026-10-26", "2026-11-19")


def test_overview_default_is_8_weeks_and_rejects_other_values(monkeypatch):
    c = _setup(monkeypatch, FakeClient(results=RESULTS))
    assert len(c.get("/reports/overview").json()["cohorts"]) == 8
    assert c.get("/reports/overview?weeks=5").status_code == 422


def test_overview_program_filter_reaches_every_block(monkeypatch):
    client = FakeClient(results=RESULTS)
    assert _setup(monkeypatch, client).get(f"/reports/overview?programId={P1}").status_code == 200
    for name in ("report_cohort_weeks", "report_activity", "report_campaigns",
                 "report_levels", "report_branches"):
        assert client.params(name)[0]["p_program"] == P1, name


def test_overview_starter_omits_branches(monkeypatch):
    client = FakeClient(tier="starter", results=RESULTS)
    body = _setup(monkeypatch, client).get("/reports/overview").json()
    assert "branches" not in body
    assert client.params("report_branches") == []


def test_overview_branch_filter_needs_branch_reports(monkeypatch):
    starter = _setup(monkeypatch, FakeClient(tier="starter", results=RESULTS))
    r = starter.get(f"/reports/overview?locationId={L1}")
    assert r.status_code == 402
    assert r.json()["details"]["feature"] == "branch_reports"
    growth = _setup(monkeypatch, FakeClient(tier="growth", results=RESULTS))
    assert growth.get(f"/reports/overview?locationId={L1}").status_code == 402


def test_overview_branch_filter_for_pro(monkeypatch):
    client = FakeClient(results=RESULTS)
    assert _setup(monkeypatch, client).get(f"/reports/overview?locationId={L2}").status_code == 200
    assert all(p["p_locations"] == [L2] for _, p in client.calls)


def test_overview_manager_scope_and_explicit_branch(monkeypatch):
    client = FakeClient(staff_role="manager", scope=(L1, L2), results=RESULTS)
    c = _setup(monkeypatch, client, roles=("operation_user",))
    body = c.get("/reports/overview").json()
    assert "branches" in body
    assert all(p["p_locations"] == [L1, L2] for _, p in client.calls)

    client.calls.clear()
    assert c.get(f"/reports/overview?locationId={L2}").status_code == 200
    assert all(p["p_locations"] == [L2] for _, p in client.calls)

    client.calls.clear()
    assert c.get(f"/reports/overview?locationId={L3}").status_code == 403
    assert client.calls == []


def test_overview_manager_without_branches_gets_empty_scope(monkeypatch):
    client = FakeClient(staff_role="manager", scope=(), results={})
    body = _setup(monkeypatch, client, roles=("operation_user",)).get("/reports/overview").json()
    assert client.params("report_activity")[0]["p_locations"] == []
    assert body["kpis"]["newCustomers"] == 0


def test_overview_admin_uses_merchant_tier(monkeypatch):
    client = FakeClient(tier="starter", results=RESULTS)
    c = _setup(monkeypatch, client, roles=("platform_admin",), merchant_id=None)
    assert c.get("/reports/overview").status_code == 422
    body = c.get(f"/reports/overview?merchantId={M1}").json()
    assert "branches" not in body


# --------------------------------------------------------------------------- #
# /reports/export
# --------------------------------------------------------------------------- #
def test_export_needs_data_export(monkeypatch):
    for tier in ("starter", "growth"):
        c = _setup(monkeypatch, FakeClient(tier=tier, results=RESULTS))
        r = c.get("/reports/export?section=cohorts")
        assert r.status_code == 402, tier
        assert r.json()["details"]["feature"] == "data_export"


def test_export_cohorts_csv(monkeypatch):
    c = _setup(monkeypatch, FakeClient(results=RESULTS))
    r = c.get("/reports/export?section=cohorts&weeks=4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == \
        'attachment; filename="conpass-cohorts-2026-11-19.csv"'
    assert r.content.startswith(b"\xef\xbb\xbf")
    lines = r.content.decode("utf-8-sig").split("\r\n")
    assert lines[0] == "Semana del,Nuevos,Volvieron,Segunda visita (%),Estado"
    assert lines[1] == "2026-10-26,0,0,,Cerrada" or lines[1] == "2026-10-26,0,0,,En curso"
    assert lines[3] == "2026-11-09,5,2,40.0,En curso"
    assert lines[-1] == ""


def test_export_campaigns_levels_branches(monkeypatch):
    c = _setup(monkeypatch, FakeClient(results=RESULTS))
    camp = c.get("/reports/export?section=campaigns").content.decode("utf-8-sig").split("\r\n")
    assert camp[0] == "Campaña,Tipo,Visitas,Canjes,Segunda visita (%)"
    assert camp[1] == "Café,Sellos,30,4,37.5"
    assert camp[2] == "Navidad,Descuento,10,2,"
    lev = c.get("/reports/export?section=levels").content.decode("utf-8-sig").split("\r\n")
    assert lev[:3] == ["Nivel,Tarjetas activas", "1,9", "2,3"]
    br = c.get("/reports/export?section=branches").content.decode("utf-8-sig").split("\r\n")
    assert br[:2] == ["Sucursal,Nuevos,Visitas,Segunda visita (%)", "Centro,5,20,20.0"]


def test_export_redemptions_uses_ecuador_time_and_fallback_name(monkeypatch):
    rows = [
        {"occurred_at": "2026-11-19T03:30:00+00:00", "customer_name": "Ana Pérez",
         "program_name": "Café", "branch_name": "Centro"},
        {"occurred_at": "2026-11-18T15:05:00+00:00", "customer_name": None,
         "program_name": "Café", "branch_name": None},
    ]
    client = FakeClient(results={"report_redemptions": rows})
    r = _setup(monkeypatch, client).get(f"/reports/export?section=redemptions&programId={P1}")
    assert r.status_code == 200
    lines = r.content.decode("utf-8-sig").split("\r\n")
    assert lines[0] == "Fecha y hora (Ecuador),Cliente,Campaña,Sucursal"
    assert lines[1] == "2026-11-18 22:30,Ana Pérez,Café,Centro"
    assert lines[2] == "2026-11-18 10:05,Cliente,Café,"
    assert 'filename="conpass-redemptions-2026-11-19.csv"' in r.headers["content-disposition"]
    p = client.params("report_redemptions")[0]
    assert p["p_program"] == P1 and p["p_from"] == "2026-09-28"


def test_export_redemptions_pages_through_results(monkeypatch):
    rows = [{"occurred_at": "2026-11-18T15:05:00+00:00", "customer_name": f"C{i}",
             "program_name": "Café", "branch_name": None} for i in range(1001)]
    client = FakeClient(results={"report_redemptions": rows})
    r = _setup(monkeypatch, client).get("/reports/export?section=redemptions")
    assert len(r.content.decode("utf-8-sig").strip().split("\r\n")) == 1002
    assert len(client.params("report_redemptions")) == 2


def test_export_same_scope_as_overview(monkeypatch):
    client = FakeClient(staff_role="manager", scope=(L1,), results=RESULTS)
    c = _setup(monkeypatch, client, roles=("operation_user",))
    assert c.get("/reports/export?section=campaigns").status_code == 200
    assert all(p["p_locations"] == [L1] for _, p in client.calls)
    client.calls.clear()
    assert c.get(f"/reports/export?section=redemptions&locationId={L3}").status_code == 403
    assert c.get("/reports/export?section=nope").status_code == 422
    assert c.get("/reports/export").status_code == 422


def test_export_admin_needs_merchant(monkeypatch):
    c = _setup(monkeypatch, FakeClient(results=RESULTS), roles=("platform_admin",),
               merchant_id=None)
    assert c.get("/reports/export?section=levels").status_code == 422

