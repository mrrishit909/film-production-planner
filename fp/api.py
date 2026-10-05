"""Public API (modular monolith). Blueprint services map to: script-parser + breakdown-service (parse, approve),
cast-service (availability), location-service + resource-service (locations and fees), scheduler (CP-SAT job),
budget-service (cost items, variance), weather-service (rain chances carried by a scenario; daylight computed) and
collaboration-service (scenarios, approval, audit).

    uvicorn fp.api:app          python -m core.jobs fp.api      # the worker
"""
import datetime
import uuid

from fastapi import Depends, Header
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"production:read", "jobs:read"}
PLAN = READ | {"breakdown:write", "schedule:write", "budget:read"}
PERMISSIONS = {"crew": READ, "assistant_director": PLAN, "producer": PLAN | {"breakdown:approve", "audit:read"}}
app = create_app("film-production-planner", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
CATEGORIES = ["cast", "locations", "crew", "company_moves", "overtime", "weather_risk"]


def production(c, pid, lock=False):
    p = c.execute("SELECT id, title, start_date, budget, shoot_days, breakdown_status FROM productions WHERE id = %s" + (" FOR UPDATE" if lock else ""), [pid]).fetchone()
    if not p:
        raise Problem(404, "production_not_found")
    return p


def load(c, pid):
    """Everything the engine needs for one production: scenes with cast, rates, fees, shoot dates."""
    p = production(c, pid)
    rows = c.execute("""SELECT s.id, s.scene_no, s.int_ext, s.day_night, s.requirements, l.name AS location,
                               coalesce((SELECT array_agg(k.name ORDER BY k.name) FROM scene_cast sc JOIN "cast" k ON k.id = sc.cast_id WHERE sc.scene_id = s.id), '{}') AS cast_names
                          FROM scenes s JOIN locations l ON l.id = s.location_id WHERE s.production_id = %s ORDER BY s.ordinal""", [pid]).fetchall()
    scenes = [{"number": int(r["scene_no"]), "id": r["id"], "int_ext": r["int_ext"], "time": r["day_night"], "location": r["location"], "cast": r["cast_names"], "eighths": r["requirements"]["eighths"]} for r in rows]
    cast = c.execute('SELECT id, name, constraints, rate FROM "cast" WHERE production_id = %s ORDER BY name', [pid]).fetchall()
    fees = {r["name"]: float(r["fee"]) for r in c.execute("SELECT name, fee FROM locations WHERE production_id = %s", [pid])}
    return p, scenes, {k["name"]: k["rate"]["day"] for k in cast}, fees, engine.shoot_dates(p["start_date"], p["shoot_days"]), {k["name"]: k["constraints"]["unavailable"] for k in cast}


def scenario(c, sid, lock=False):
    s = c.execute("SELECT id, production_id, name, based_on, config, status, result FROM scenarios WHERE id = %s" + (" FOR UPDATE" if lock else ""), [sid]).fetchone()
    if not s:
        raise Problem(404, "scenario_not_found")
    return s


def plan_of(c, sid):
    return {int(r["scene_no"]): r["day_index"] for r in c.execute("SELECT s.scene_no, d.day_index FROM schedule_items i JOIN shoot_days d ON d.id = i.shoot_day_id JOIN scenes s ON s.id = i.scene_id WHERE d.scenario_id = %s", [sid])}


# ---------------------------------------------------------------- script-parser + breakdown-service

class ParseIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    source: str | None = Field(None, pattern="^demo-screenplay$", description="Use the built-in synthetic screenplay instead of sending text")
    text: str | None = Field(None, max_length=600000, description="A screenplay in standard format")
    start_date: datetime.date
    shoot_days: int = Field(24, ge=1, le=80, description="Weekdays available for shooting, from the start date")
    budget: float = Field(ge=0, le=10**9)


@app.post("/v1/scripts/parse", status_code=201, tags=["breakdown"], summary="Read a screenplay into scenes, locations and cast. The result is a draft breakdown that a person must approve before anything is scheduled")
def parse(body: ParseIn, ctx: Ctx = Depends(auth("breakdown:write")), idem: str | None = IdemKey):
    if bool(body.source) == bool(body.text):
        raise Problem(422, "source_or_text", "send either source or text, not both")

    def work(c):
        text = world.screenplay()[0] if body.source else body.text
        scenes, names, warnings = engine.parse(text)
        if not scenes:
            raise Problem(422, "no_scenes_found", "no scene heading of the form 'INT. PLACE - DAY' was found")
        pid = uuid.uuid7()
        c.execute("INSERT INTO productions (id, tenant_id, title, start_date, budget, shoot_days, parser) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  [pid, ctx.tenant_id, body.title, body.start_date, body.budget, body.shoot_days, Jsonb({"version": engine.VERSION, "warnings": warnings, "lines": text.count(chr(10)) + 1})])
        lid = {}
        for s in scenes:
            if s["location"] not in lid:
                lid[s["location"]] = uuid.uuid5(pid, "loc-" + s["location"])
                c.execute("INSERT INTO locations (id, tenant_id, production_id, name, int_ext, fee) VALUES (%s,%s,%s,%s,%s,%s)", [lid[s["location"]], ctx.tenant_id, pid, s["location"], s["int_ext"], world.FEES.get(s["location"], 4000)])
        cid = {n: uuid.uuid5(pid, "cast-" + n) for n in names}
        for n in names:
            c.execute('INSERT INTO "cast" (id, tenant_id, production_id, name, rate) VALUES (%s,%s,%s,%s,%s)', [cid[n], ctx.tenant_id, pid, n, Jsonb({"day": world.RATES.get(n, 2000), "basis": "paid from first to last work day"})])
        for k, s in enumerate(scenes):
            sid = uuid.uuid5(pid, f"scene-{s['number']}")
            c.execute("INSERT INTO scenes (id, tenant_id, production_id, scene_no, int_ext, day_night, pages, location_id, requirements, ordinal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      [sid, ctx.tenant_id, pid, str(s["number"]), s["int_ext"], s["time"], s["eighths"] / 8, lid[s["location"]], Jsonb({"eighths": s["eighths"], "voice_over": s["voice_over"], "notes": s["notes"], "minutes": engine.minutes(s)}), k])
            for n in s["cast"]:
                c.execute("INSERT INTO scene_cast VALUES (%s,%s,%s)", [sid, cid[n], ctx.tenant_id])
        audit.record(c, ctx, "script.parsed", "production", pid, {"scenes": len(scenes), "cast": len(names), "parser": engine.VERSION})
        counts = {}
        for s in scenes:
            for n in s["cast"]:
                counts[n] = counts.get(n, 0) + 1
        flagged = [{"scene": s["number"], "notes": s["notes"]} for s in scenes if s["notes"]]
        return 201, {"production_id": pid, "title": body.title, "breakdown_status": "draft", "scenes": len(scenes), "pages": round(sum(s["eighths"] for s in scenes) / 8, 1), "locations": len(lid), "cast": len(names),
                     "interior": sum(s["int_ext"] == "INT" for s in scenes), "exterior": sum(s["int_ext"] == "EXT" for s in scenes), "night": sum(s["time"] == "NIGHT" for s in scenes), "shooting_minutes": sum(engine.minutes(s) for s in scenes),
                     "scenes_by_cast": sorted(counts.items(), key=lambda kv: -kv[1]), "for_review": flagged[:12], "scenes_flagged": len(flagged), "warnings": warnings,
                     "sample": [{"scene": s["number"], "set": f"{s['int_ext']}. {s['location']} - {s['time']}", "pages": s["eighths"] / 8, "cast": s["cast"], "voice_over": s["voice_over"]} for s in scenes[:6]],
                     "method": "rules for standard screenplay format; no language model", "next": "a producer approves the breakdown: POST /v1/productions/{id}/breakdown:approve"}
    return run(ctx, idem, body, work)


@app.post("/v1/productions/{production_id}/breakdown:approve", tags=["breakdown"], summary="(+) A person confirms the parsed breakdown. Scheduling is refused until this is done")
def approve(production_id: uuid.UUID, ctx: Ctx = Depends(auth("breakdown:approve")), idem: str | None = IdemKey):
    def work(c):
        p = production(c, production_id, lock=True)
        c.execute("UPDATE productions SET breakdown_status = 'approved' WHERE id = %s", [production_id])
        audit.record(c, ctx, "breakdown.approved", "production", production_id, {"was": p["breakdown_status"]})
        return 200, {"production_id": production_id, "breakdown_status": "approved", "approved_by": ctx.actor_name}
    return run(ctx, idem, {}, work)


# ---------------------------------------------------------------- cast-service + scenarios

class AvailabilityIn(BaseModel):
    production_id: uuid.UUID
    cast: str = Field(min_length=1, max_length=80)
    unavailable: list[datetime.date] = Field(max_length=200)


@app.post("/v1/availability", tags=["cast"], summary="Set the dates a cast member cannot work (replaces the earlier list). New scenarios start from these")
def availability(body: AvailabilityIn, ctx: Ctx = Depends(auth("schedule:write")), idem: str | None = IdemKey):
    def work(c):
        production(c, body.production_id)
        dates = sorted({d.isoformat() for d in body.unavailable})
        row = c.execute('UPDATE "cast" SET constraints = %s WHERE production_id = %s AND name = %s RETURNING id', [Jsonb({"unavailable": dates}), body.production_id, body.cast.upper()]).fetchone()
        if not row:
            raise Problem(404, "cast_member_not_found")
        audit.record(c, ctx, "availability.set", "cast", row["id"], {"days": len(dates)})
        return 200, {"cast": body.cast.upper(), "unavailable": dates}
    return run(ctx, idem, body, work)


class ScenarioIn(BaseModel):
    production_id: uuid.UUID
    name: str = Field(min_length=1, max_length=80)
    based_on: uuid.UUID | None = Field(None, description="An earlier scenario whose schedule this one should stay close to")
    unavailable: dict[str, list[datetime.date]] = Field(default_factory=dict, description="Extra unavailable dates per cast member, on top of POST /v1/availability")
    rain: dict[datetime.date, float] = Field(default_factory=dict, description="Chance of rain by date, 0 to 1")


@app.post("/v1/scenarios", status_code=201, tags=["scenarios"], summary="Create a what-if: its own cast availability and weather. Solving it does not touch any other scenario")
def create_scenario(body: ScenarioIn, ctx: Ctx = Depends(auth("schedule:write")), idem: str | None = IdemKey):
    def work(c):
        _, _, rates, _, dates, base = load(c, body.production_id)
        if body.based_on and scenario(c, body.based_on)["production_id"] != body.production_id:
            raise Problem(422, "based_on_another_production")
        unknown = [n for n in body.unavailable if n.upper() not in rates]
        if unknown or any(not 0 <= p <= 1 for p in body.rain.values()):
            raise Problem(422, "bad_scenario", f"unknown cast: {unknown}" if unknown else "rain chances are between 0 and 1")
        un = {n: sorted(set(base[n]) | {d.isoformat() for d in body.unavailable.get(n, body.unavailable.get(n.title(), []))}) for n in rates}
        for n, ds in body.unavailable.items():
            un[n.upper()] = sorted(set(un[n.upper()]) | {d.isoformat() for d in ds})
        sid = uuid.uuid7()
        cfg = {"unavailable": {n: v for n, v in un.items() if v}, "rain": {d.isoformat(): p for d, p in body.rain.items()}}
        c.execute("INSERT INTO scenarios (id, tenant_id, production_id, name, based_on, config) VALUES (%s,%s,%s,%s,%s,%s)", [sid, ctx.tenant_id, body.production_id, body.name, body.based_on, Jsonb(cfg)])
        audit.record(c, ctx, "scenario.created", "scenario", sid, {"name": body.name, "based_on": str(body.based_on) if body.based_on else None})
        return 201, {"scenario_id": sid, "name": body.name, "based_on": body.based_on, **cfg, "shoot_window": [dates[0].isoformat(), dates[-1].isoformat()]}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- scheduler + budget-service

class OptimizeIn(BaseModel):
    scenario_id: uuid.UUID
    seconds: float = Field(12, ge=1, le=60, description="Time the solver may spend; it returns the best schedule found")


@app.post("/v1/schedules/optimize", status_code=202, tags=["schedule"], summary="Solve a scenario: every scene on a shoot day at the lowest cost under the rules, staying close to the scenario it is based on (job)")
def optimize(body: OptimizeIn, ctx: Ctx = Depends(auth("schedule:write")), idem: str | None = IdemKey):
    def work(c):
        s = scenario(c, body.scenario_id)
        if production(c, s["production_id"])["breakdown_status"] != "approved":
            raise Problem(409, "breakdown_not_approved", "a producer must approve the breakdown before it is scheduled")
        return 202, jobs.enqueue(c, ctx, "schedule.optimize", body.model_dump(mode="json"))
    return run(ctx, idem, body, work)


@jobs.handler("schedule.optimize")
def optimize_job(c, job):
    t = job["tenant_id"]
    s = scenario(c, job["payload"]["scenario_id"], lock=True)
    p, scenes, rates, fees, dates, _ = load(c, s["production_id"])
    un, rain = s["config"]["unavailable"], s["config"]["rain"]
    blockers = engine.impossible(scenes, dates, un)
    if blockers:
        c.execute("UPDATE scenarios SET status = 'infeasible', result = %s WHERE id = %s", [Jsonb({"reasons": blockers}), s["id"]])
        return {"scenario_id": s["id"], "name": s["name"], "status": "infeasible", "reasons": blockers, "note": "nothing was scheduled; the earlier scenarios are unchanged"}
    previous = plan_of(c, s["based_on"]) if s["based_on"] else None
    baseline = engine.script_order(scenes, dates, un)
    plan, status, bound = engine.schedule(scenes, dates, rates, fees, un, rain, previous, job["payload"]["seconds"], hint=previous or baseline)
    if plan is None:
        c.execute("UPDATE scenarios SET status = 'infeasible', result = %s WHERE id = %s", [Jsonb({"reasons": [f"solver status {status}"]}), s["id"]])
        return {"scenario_id": s["id"], "name": s["name"], "status": "infeasible", "reasons": [f"no schedule satisfies every rule in {p['shoot_days']} shoot days (solver: {status})"]}
    broken = engine.violations(scenes, plan, dates, un)
    if broken:                                                   # the solver's answer is re-checked by independent code before it is stored
        raise RuntimeError("; ".join(broken[:3]))
    cost = engine.cost(scenes, plan, dates, rates, fees, rain)
    base_cost = engine.cost(scenes, baseline, dates, rates, fees, rain) if baseline else None
    c.execute("DELETE FROM shoot_days WHERE scenario_id = %s", [s["id"]])
    c.execute("DELETE FROM cost_items WHERE scenario_id = %s", [s["id"]])
    lid = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM locations WHERE production_id = %s", [p["id"]])}
    by_scene = {x["number"]: x for x in scenes}
    for d in sorted(set(plan.values())):
        sheet = engine.call_sheet(scenes, plan, dates, d, rain)
        did = uuid.uuid5(s["id"], f"day-{d}")
        c.execute("INSERT INTO shoot_days (id, tenant_id, production_id, shoot_date, call_time, status, location_id, scenario_id, day_index) VALUES (%s,%s,%s,%s,%s,'planned',%s,%s,%s)", [did, t, p["id"], dates[d], sheet["crew_call"], lid[sheet["locations"][0]], s["id"], d])
        for seq, row in enumerate(sheet["scenes"], 1):
            c.execute("INSERT INTO schedule_items VALUES (%s,%s,%s,%s,%s)", [did, by_scene[row["scene"]]["id"], seq, row["minutes"], t])
    for cat in CATEGORIES:
        c.execute("INSERT INTO cost_items (id, tenant_id, production_id, category, amount, scenario_id) VALUES (%s,%s,%s,%s,%s,%s)", [uuid.uuid7(), t, p["id"], cat, cost[cat], s["id"]])
    used = sorted(set(plan.values()))
    out = {"scenario_id": s["id"], "name": s["name"], "status": "scheduled", "solver": {"status": status, "seconds": job["payload"]["seconds"], "lower_bound": round(bound), "note": "best schedule found in the time allowed; not proven to be the cheapest possible"},
           "shoot_days": cost["shoot_days"], "first_date": dates[used[0]].isoformat(), "last_date": dates[used[-1]].isoformat(), "night_shoots": len({d for n, d in plan.items() if by_scene[n]["time"] == "NIGHT"}),
           "costs": {k: cost[k] for k in CATEGORIES}, "total": cost["total"], "budget": float(p["budget"]), "over_budget_by": max(0, cost["total"] - float(p["budget"])), "overtime_days": cost["overtime_days"],
           "cast_days": sorted(({"cast": a, "work_days": cost["cast_work_days"][a], "paid_days": cost["cast_span_days"][a], "idle_paid_days": cost["cast_span_days"][a] - cost["cast_work_days"][a]} for a in cost["cast_span_days"]), key=lambda r: -r["paid_days"]),
           "script_order": {"total": base_cost["total"], "shoot_days": base_cost["shoot_days"], "costs": {k: base_cost[k] for k in CATEGORIES}} if base_cost else None, "rules_checked": "day length, two locations, day or night, daylight for exteriors, availability, turnaround: 0 violations"}
    if previous:
        was = c.execute("SELECT name, result FROM scenarios WHERE id = %s", [s["based_on"]]).fetchone()
        moved = sorted(n for n in plan if previous.get(n) != plan[n])
        out["against"] = {"scenario": was["name"], "scenes_moved": len(moved), "moved": [{"scene": n, "from": dates[previous[n]].isoformat(), "to": dates[plan[n]].isoformat(), "set": f"{by_scene[n]['int_ext']}. {by_scene[n]['location']}", "cast": by_scene[n]["cast"]} for n in moved[:40]],
                          "extra_cost": cost["total"] - was["result"]["total"], "extra_days": cost["shoot_days"] - was["result"]["shoot_days"],
                          "exterior_scenes_on_rain_days": sum(1 for n, d in plan.items() if by_scene[n]["int_ext"] == "EXT" and rain.get(dates[d].isoformat())),
                          "had_to_move": sum(1 for n, d in previous.items() if any(dates[d].isoformat() in un.get(a, ()) for a in by_scene[n]["cast"]))}
    c.execute("UPDATE scenarios SET status = 'scheduled', result = %s WHERE id = %s", [Jsonb(jsonable_encoder(out)), s["id"]])
    audit.record(c, Ctx(t, uuid.UUID(job["payload"]["actor_id"]), "worker", "system"), "schedule.solved", "scenario", s["id"], {"name": s["name"], "total": cost["total"], "days": cost["shoot_days"], "solver": status, "engine": engine.VERSION})
    return out


@app.get("/v1/schedules/{scenario_id}", tags=["schedule"], summary="(+) The stripboard: each shoot day with its scenes in order")
def stripboard(scenario_id: uuid.UUID, ctx: Ctx = Depends(auth("production:read"))):
    with db.tx(ctx.tenant_id) as c:
        s = scenario(c, scenario_id)
        rows = c.execute("""SELECT d.day_index, d.shoot_date, d.call_time, i.sequence, i.estimated_minutes, s.scene_no, s.int_ext, s.day_night, s.pages, l.name AS location
                              FROM shoot_days d JOIN schedule_items i ON i.shoot_day_id = d.id JOIN scenes s ON s.id = i.scene_id JOIN locations l ON l.id = s.location_id WHERE d.scenario_id = %s ORDER BY d.day_index, i.sequence""", [scenario_id]).fetchall()
    days = {}
    for r in rows:
        day = days.setdefault(r["day_index"], {"day": len(days) + 1, "date": r["shoot_date"], "crew_call": r["call_time"], "minutes": 0, "night": r["day_night"] == "NIGHT", "locations": [], "scenes": []})
        day["minutes"] += r["estimated_minutes"]
        day["scenes"].append({"scene": int(r["scene_no"]), "int_ext": r["int_ext"], "location": r["location"], "pages": float(r["pages"]), "minutes": r["estimated_minutes"]})
        if r["location"] not in day["locations"]:
            day["locations"].append(r["location"])
    return jsonable_encoder({"scenario_id": scenario_id, "name": s["name"], "status": s["status"], "days": list(days.values())})


@app.get("/v1/budget/variance", tags=["budget"], summary="A scenario's cost by category against the budget, and against another scenario")
def variance(scenario_id: uuid.UUID, against: uuid.UUID | None = None, ctx: Ctx = Depends(auth("budget:read"))):
    with db.tx(ctx.tenant_id) as c:
        s = scenario(c, scenario_id)
        p = production(c, s["production_id"])
        other = scenario(c, against) if against else None
        costs = lambda sid: {r["category"]: float(r["amount"]) for r in c.execute("SELECT category, amount FROM cost_items WHERE scenario_id = %s", [sid])}      # noqa: E731
        mine, theirs = costs(scenario_id), costs(against) if against else {}
    if not mine or (against and not theirs):
        raise Problem(409, "not_scheduled", "solve the scenario first: POST /v1/schedules/optimize")
    total = sum(mine.values())
    return jsonable_encoder({"scenario": s["name"], "against": other["name"] if other else None, "budget": p["budget"], "total": total, "budget_variance": total - float(p["budget"]), "within_budget": total <= float(p["budget"]),
                             "lines": [{"category": k, "amount": mine[k], "other": theirs.get(k), "difference": mine[k] - theirs[k] if theirs else None} for k in CATEGORIES], "total_difference": total - sum(theirs.values()) if theirs else None,
                             "assumptions": {"crew_per_shoot_day": engine.CREW_DAY, "company_move": engine.MOVE_COST, "overtime_per_minute": engine.OT_PER_MIN, "rain_cost_per_exterior_minute": engine.RAIN_PER_MIN,
                                             "cast": "day rate x days from first to last work day", "note": "weather_risk is an expected cost (chance of rain x exterior minutes), not money spent"}})


@app.get("/v1/call-sheets/{date}", tags=["schedule"], summary="The call sheet for one shoot date in a scenario: scenes in order, cast calls, daylight and rain risk")
def call_sheet(date: datetime.date, scenario_id: uuid.UUID, ctx: Ctx = Depends(auth("production:read"))):
    with db.tx(ctx.tenant_id) as c:
        s = scenario(c, scenario_id)
        _, scenes, _, _, dates, _ = load(c, s["production_id"])
        plan = plan_of(c, scenario_id)
    if date not in dates or dates.index(date) not in plan.values():
        raise Problem(404, "no_shooting_that_day", "this scenario has no scenes scheduled on that date")
    return {"scenario": s["name"], **engine.call_sheet(scenes, plan, dates, dates.index(date), s["config"]["rain"])}
