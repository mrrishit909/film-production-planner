"""Integration, end-to-end and security tests against a real Postgres."""
import pytest

from core import db, jobs, scenario
from fp import engine, world

from .conftest import bearer

PRODUCER, AD, CREW, OTHER = bearer("studio", "producer"), bearer("studio", "assistant_director"), bearer("studio", "crew"), bearer("other", "producer")
ZERO = "00000000-0000-0000-0000-000000000000"
SMALL = "INT. DINER - DAY\n\nMara waits.\n\n                    MARA\n          Sit down.\n\nEXT. ROOFTOP - NIGHT\n\n                    JONAH\n          I would rather stand.\n"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=()):
    with db.tx() as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    _, truth = world.screenplay()
    p = demo["parse"]
    assert p["scenes"] == 85 and p["breakdown_status"] == "draft" and p["cast"] == 12 and p["locations"] == 14 and p["warnings"] == [] and p["pages"] == round(sum(x["eighths"] for x in truth) / 8, 1)
    assert dict(p["scenes_by_cast"])["MARA"] == sum("MARA" in x["cast"] for x in truth) and demo["ok"]["breakdown_status"] == "approved"
    a, b = demo["opt_a"]["result"], demo["opt_b"]["result"]
    for r in (a, b):
        assert r["status"] == "scheduled" and r["total"] == sum(r["costs"].values()) and r["total"] < r["script_order"]["total"] and r["solver"]["lower_bound"] <= r["total"] + 1e5
        assert all(d["paid_days"] >= d["work_days"] for d in r["cast_days"]) and all(o["minutes_over"] <= engine.MAX_MIN - engine.DAY_MIN for o in r["overtime_days"])
    board = demo["board_a"]["days"]
    assert len(board) == a["shoot_days"] and sum(len(d["scenes"]) for d in board) == 85 and all(d["minutes"] <= engine.MAX_MIN and len(d["locations"]) <= 2 for d in board)
    sheet = demo["sheet"]
    assert sheet["date"] == a["first_date"] and sheet["day_number"] == 1 and sheet["scenes"] and sheet["cast_calls"] and sheet["sunrise"] < sheet["sunset"]
    ag = b["against"]
    assert ag["scenario"] == "Plan A" and ag["scenes_moved"] >= ag["had_to_move"] > 0 and ag["exterior_scenes_on_rain_days"] == 0 and ag["scenes_moved"] < 60 and ag["extra_cost"] == b["total"] - a["total"]
    var = demo["var"]
    assert var["total"] == b["total"] and var["total_difference"] == ag["extra_cost"] and sum(x["difference"] for x in var["lines"]) == pytest.approx(var["total_difference"]) and var["within_budget"] == (b["total"] <= 1300000)
    assert demo["sheet_b"]["rain_chance"] == 0.6 and not demo["sheet_b"]["exterior_work"]
    c = demo["opt_c"]["result"]
    assert c["status"] == "infeasible" and c["reasons"] == ["MARA has scenes but is unavailable on every shoot day"]
    assert demo["audit"]["chain_valid"] and {"script.parsed", "breakdown.approved", "schedule.solved"} <= {e["action"] for e in demo["audit"]["events"]}


def test_the_stored_schedules_break_no_rule_and_keep_the_lead_off_her_days(demo):
    with db.tx() as c:
        tenant = c.execute("SELECT id FROM tenants WHERE name = 'Low Tide Pictures'").fetchone()["id"]
    for key, un in (("a", {}), ("b", {"MARA": ["2026-11-06", "2026-11-09", "2026-11-10"]})):
        with db.tx(tenant) as c:
            from fp import api
            _, scenes, _, _, dates, _ = api.load(c, demo["parse"]["production_id"])
            plan = api.plan_of(c, demo[key]["scenario_id"])
        assert engine.violations(scenes, plan, dates, un) == [] and len(plan) == 85
    assert one("""SELECT count(*) AS n FROM shoot_days d JOIN schedule_items i ON i.shoot_day_id = d.id JOIN scene_cast sc ON sc.scene_id = i.scene_id JOIN "cast" k ON k.id = sc.cast_id
                   WHERE d.scenario_id = %s AND k.name = 'MARA' AND d.shoot_date IN ('2026-11-06', '2026-11-09', '2026-11-10')""", [demo["b"]["scenario_id"]])["n"] == 0
    assert one("SELECT count(*) AS n FROM shoot_days WHERE scenario_id = %s", [demo["c"]["scenario_id"]])["n"] == 0             # the impossible plan stored nothing
    assert one("SELECT count(*) AS n FROM shoot_days WHERE scenario_id = %s", [demo["a"]["scenario_id"]])["n"] == demo["opt_a"]["result"]["shoot_days"]      # and solving B left A alone


def test_nothing_is_scheduled_until_a_person_approves_the_breakdown(client, demo):
    made = client.post("/v1/scripts/parse", headers=AD, json={"title": "Short", "text": SMALL, "start_date": "2026-11-02", "shoot_days": 3, "budget": 200000})
    assert made.status_code == 201 and made.json()["scenes"] == 2 and made.json()["sample"][0]["cast"] == ["MARA"]
    pid = made.json()["production_id"]
    sc = client.post("/v1/scenarios", headers=AD, json={"production_id": pid, "name": "first"}).json()["scenario_id"]
    assert client.post("/v1/schedules/optimize", headers=AD, json={"scenario_id": sc, "seconds": 2}).json()["code"] == "breakdown_not_approved"
    assert client.post(f"/v1/productions/{pid}/breakdown:approve", headers=AD).status_code == 403                 # the person who parsed it cannot approve it
    assert client.post(f"/v1/productions/{pid}/breakdown:approve", headers=PRODUCER).status_code == 200
    assert client.post("/v1/availability", headers=AD, json={"production_id": pid, "cast": "jonah", "unavailable": ["2026-11-02"]}).json() == {"cast": "JONAH", "unavailable": ["2026-11-02"]}
    sc2 = client.post("/v1/scenarios", headers=AD, json={"production_id": pid, "name": "second"}).json()
    assert sc2["unavailable"] == {"JONAH": ["2026-11-02"]}                                                         # a new scenario starts from the standing availability
    job = client.post("/v1/schedules/optimize", headers=AD, json={"scenario_id": sc2["scenario_id"], "seconds": 2}).json()
    jobs.drain()
    out = client.get(job["status_url"], headers=AD).json()["result"]
    assert out["status"] == "scheduled" and out["solver"]["status"] == "OPTIMAL" and out["shoot_days"] == 2 and out["first_date"] == "2026-11-02"
    night = client.get("/v1/call-sheets/2026-11-03", headers=CREW, params={"scenario_id": sc2["scenario_id"]}).json()
    assert night["night_shoot"] and night["cast_calls"][0]["cast"] == "JONAH"
    assert client.get("/v1/call-sheets/2026-11-04", headers=CREW, params={"scenario_id": sc2["scenario_id"]}).status_code == 404


@pytest.mark.parametrize("headers,method,path,body,expected", [
    ({}, "GET", f"/v1/schedules/{ZERO}", None, 401),
    (CREW, "GET", f"/v1/schedules/{ZERO}", None, 404),
    (CREW, "POST", "/v1/scripts/parse", {"title": "x", "source": "demo-screenplay", "start_date": "2026-11-02", "budget": 1}, 403),
    (CREW, "GET", f"/v1/budget/variance?scenario_id={ZERO}", None, 403),
    (CREW, "POST", "/v1/schedules/optimize", {"scenario_id": ZERO}, 403),
    (AD, "GET", "/v1/audit", None, 403),
    (AD, "POST", "/v1/scripts/parse", {"title": "x", "start_date": "2026-11-02", "budget": 1}, 422),
    (AD, "POST", "/v1/scripts/parse", {"title": "x", "text": "nothing like a script", "start_date": "2026-11-02", "budget": 1}, 422),
    (AD, "POST", "/v1/scripts/parse", {"title": "x", "source": "http://internal/script", "start_date": "2026-11-02", "budget": 1}, 422),
    (AD, "POST", "/v1/schedules/optimize", {"scenario_id": ZERO}, 404),
    (AD, "POST", "/v1/schedules/optimize", {"scenario_id": ZERO, "seconds": 9000}, 422),
    (AD, "POST", "/v1/scenarios", {"production_id": ZERO, "name": "x"}, 404),
    (AD, "POST", "/v1/availability", {"production_id": ZERO, "cast": "MARA", "unavailable": []}, 404),
])
def test_authorization_and_validation(client, demo, headers, method, path, body, expected):
    assert client.request(method, path, headers=headers, json=body).status_code == expected


def test_scenario_validation(client, demo):
    pid = demo["parse"]["production_id"]
    assert client.post("/v1/scenarios", headers=AD, json={"production_id": pid, "name": "x", "unavailable": {"NOBODY": ["2026-11-02"]}}).json()["code"] == "bad_scenario"
    assert client.post("/v1/scenarios", headers=AD, json={"production_id": pid, "name": "x", "rain": {"2026-11-02": 1.5}}).json()["code"] == "bad_scenario"
    assert client.get("/v1/budget/variance", headers=PRODUCER, params={"scenario_id": demo["c"]["scenario_id"]}).json()["code"] == "not_scheduled"


def test_one_studio_cannot_see_anothers_production(client, demo):
    pid, sid = demo["parse"]["production_id"], demo["a"]["scenario_id"]
    for method, path, body in (("GET", f"/v1/schedules/{sid}", None), ("GET", f"/v1/budget/variance?scenario_id={sid}", None), ("GET", f"/v1/call-sheets/2026-11-03?scenario_id={sid}", None),
                               ("POST", "/v1/scenarios", {"production_id": pid, "name": "theirs"}), ("POST", f"/v1/productions/{pid}/breakdown:approve", None), ("POST", "/v1/schedules/optimize", {"scenario_id": sid})):
        assert client.request(method, path, headers=OTHER, json=body).status_code == 404
    theirs = client.post("/v1/scripts/parse", headers=OTHER, json={"title": "Theirs", "text": SMALL, "start_date": "2026-11-02", "shoot_days": 3, "budget": 1}).json()
    assert theirs["production_id"] != pid and one("SELECT count(*) AS n FROM scenes WHERE production_id = %s", [pid])["n"] == 85
