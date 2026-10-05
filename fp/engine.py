"""Screenplay breakdown, shoot-day scheduling (CP-SAT), costing, call sheets and daylight. The business rules are
generic stand-ins (a 10-hour day with overtime to 12, no day call after a night shoot, two locations a day at most);
they are not any union's actual agreement."""
import datetime
import math
import re

from ortools.sat.python import cp_model

VERSION = "fp-1.0"
LINES_PER_PAGE = 55
MIN_PER_PAGE = {"INT": 55, "EXT": 70}       # shooting minutes per script page (assumption)
SETUP_MIN = 15                              # per scene
DAY_MIN, MAX_MIN = 600, 720                 # a 10-hour shooting day; overtime up to 12
CREW_DAY, MOVE_COST, OT_PER_MIN, RAIN_PER_MIN = 38000, 6000, 170, 250       # dollars (assumptions)
STAY_PUT = 3000                             # what moving an already-scheduled scene is worth avoiding; not a budget item
LATITUDE = 34.05                            # for sunrise and sunset
SLUG = re.compile(r"^(INT|EXT)\. (.+?) - (DAY|NIGHT)$")
CUE = re.compile(r"^\s{15,}([A-Z][A-Z .']+?)(?:\s*\((V\.O\.|O\.S\.|CONT'D)\))?$")


# ---------------------------------------------------------------- breakdown

def parse(text):
    """Break a screenplay into scenes. A character is on set if they have a cue that is not voice-over, or if a known
    character's name appears in an action line. A name spoken inside dialogue is not a person on set.
    -> (scenes [{number, int_ext, location, time, cast, eighths, voice_over, notes}], characters, warnings)"""
    lines = text.splitlines()
    names = {m[1].strip() for ln in lines if (m := CUE.match(ln)) and not ln.strip().endswith(":")}
    order = sorted(names, key=len, reverse=True)
    scenes, cur, warnings = [], None, []
    for k, ln in enumerate(lines):
        m = SLUG.match(ln)
        if m:
            cur = {"number": len(scenes) + 1, "int_ext": m[1], "location": m[2], "time": m[3], "cast": set(), "voice_over": set(), "lines": 0}
            scenes.append(cur)
        elif re.match(r"^(INT|EXT)[./ ]", ln):
            warnings.append(f"line {k + 1}: looks like a scene heading but could not be read: {ln.strip()[:60]}")
        if cur is None:
            continue
        cur["lines"] += 1
        cue = CUE.match(ln)
        if cue and not ln.strip().endswith(":"):
            (cur["voice_over"] if cue[2] == "V.O." else cur["cast"]).add(cue[1].strip())
        elif ln and not ln.startswith(" ") and not m:             # an action line: flush left (the scene heading itself may contain a name, as in MARA'S APARTMENT)
            low = ln.upper()
            for n in order:                                       # longest names first, so "YOUNG MARA" is not also read as "MARA"
                low, hits = re.subn(rf"(?<![A-Z]){re.escape(n)}(?![A-Z])", " ", low)
                if hits:
                    cur["cast"].add(n)
    for s in scenes:
        s["eighths"] = max(1, round(s.pop("lines") * 8 / LINES_PER_PAGE))
        s["voice_over"] = sorted(s["voice_over"] - s["cast"])
        s["cast"] = sorted(s["cast"])
        s["notes"] = [f"{v} is heard in voice-over only: not called to set" for v in s["voice_over"]] + (["no speaking or named character found: check"] if not s["cast"] else [])
    return scenes, sorted(names), warnings


def parse_naive(text):
    """Baseline: every character cue in a scene is a cast member; action lines are ignored."""
    scenes, cur = [], None
    for ln in text.splitlines():
        if SLUG.match(ln):
            cur = set()
            scenes.append(cur)
        elif cur is not None and (m := CUE.match(ln)) and not ln.strip().endswith(":"):
            cur.add(m[1].strip())
    return [sorted(s) for s in scenes]


def minutes(scene):
    return round(scene["eighths"] / 8 * MIN_PER_PAGE[scene["int_ext"]]) + SETUP_MIN


# ---------------------------------------------------------------- daylight

def daylight(date, latitude=LATITUDE):
    """Sunrise and sunset as local solar time (hours) and minutes of daylight, from the standard declination formula."""
    n = date.timetuple().tm_yday
    decl = math.radians(-23.44) * math.cos(2 * math.pi * (n + 10) / 365)
    half = math.degrees(math.acos(max(-1.0, min(1.0, -math.tan(math.radians(latitude)) * math.tan(decl))))) / 15
    return 12 - half, 12 + half, round(2 * half * 60)


def shoot_dates(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- costing

def cost(scenes, plan, dates, rates, fees, rain):
    """plan: {scene number: day index}. -> {category: dollars, total, days, ...} using the same rules the solver optimises."""
    by_day = {}
    for s in scenes:
        by_day.setdefault(plan[s["number"]], []).append(s)
    cast_days, out = {}, {"cast": 0, "locations": 0, "crew": 0, "company_moves": 0, "overtime": 0, "weather_risk": 0}
    over = []
    for d, ss in by_day.items():
        mins = sum(minutes(s) for s in ss)
        locs = {s["location"] for s in ss}
        out["crew"] += CREW_DAY
        out["locations"] += sum(fees[loc] for loc in locs)
        out["company_moves"] += MOVE_COST * (len(locs) - 1)
        out["overtime"] += OT_PER_MIN * max(0, mins - DAY_MIN)
        out["weather_risk"] += sum(round(rain.get(dates[d].isoformat(), 0) * minutes(s) * RAIN_PER_MIN) for s in ss if s["int_ext"] == "EXT")
        if mins > DAY_MIN:
            over.append({"date": dates[d].isoformat(), "minutes_over": mins - DAY_MIN})
        for s in ss:
            for a in s["cast"]:
                cast_days.setdefault(a, []).append(d)
    for a, ds in cast_days.items():
        out["cast"] += rates[a] * (max(ds) - min(ds) + 1)             # paid from first to last work day, idle days included
    return {**out, "total": sum(out.values()), "shoot_days": len(by_day), "overtime_days": over, "cast_span_days": {a: max(ds) - min(ds) + 1 for a, ds in cast_days.items()},
            "cast_work_days": {a: len(set(ds)) for a, ds in cast_days.items()}}


def violations(scenes, plan, dates, unavailable):
    """Every rule the schedule must satisfy, checked independently of the solver. -> list of strings (empty = valid)"""
    out, by_day = [], {}
    for s in scenes:
        if s["number"] not in plan:
            out.append(f"scene {s['number']} is not scheduled")
            continue
        by_day.setdefault(plan[s["number"]], []).append(s)
    for d, ss in sorted(by_day.items()):
        day = dates[d].isoformat()
        mins = sum(minutes(s) for s in ss)
        if mins > MAX_MIN:
            out.append(f"{day}: {mins} minutes exceeds the {MAX_MIN}-minute day")
        if len({s["location"] for s in ss}) > 2:
            out.append(f"{day}: more than two locations")
        if len({s["time"] for s in ss}) > 1:
            out.append(f"{day}: day and night scenes on the same call")
        ext = sum(minutes(s) for s in ss if s["int_ext"] == "EXT" and s["time"] == "DAY")
        if ext > daylight(dates[d])[2] - 60:
            out.append(f"{day}: {ext} minutes of exterior day work but only {daylight(dates[d])[2]} minutes of daylight")
        for s in ss:
            for a in s["cast"]:
                if day in unavailable.get(a, ()):
                    out.append(f"{day}: {a} is not available (scene {s['number']})")
        nxt = by_day.get(d + 1)
        if ss[0]["time"] == "NIGHT" and nxt and nxt[0]["time"] == "DAY" and (dates[d + 1] - dates[d]).days == 1:
            out.append(f"{day}: a day call follows a night shoot with no turnaround")
    return out


# ---------------------------------------------------------------- scheduling

def script_order(scenes, dates, unavailable):
    """Baseline: take scenes in script order and put each on the first day where it fits every rule."""
    plan, days = {}, [{"min": 0, "locs": set(), "time": None, "ext": 0} for _ in dates]
    for s in scenes:
        m = minutes(s)
        for d, day in enumerate(days):
            date = dates[d].isoformat()
            before, after = days[d - 1] if d else None, days[d + 1] if d + 1 < len(days) else None
            if (day["min"] + m <= DAY_MIN and len(day["locs"] | {s["location"]}) <= 2 and day["time"] in (None, s["time"]) and not any(date in unavailable.get(a, ()) for a in s["cast"])
                    and (s["int_ext"] != "EXT" or s["time"] != "DAY" or day["ext"] + m <= daylight(dates[d])[2] - 60)
                    and not (s["time"] == "DAY" and before and before["time"] == "NIGHT" and (dates[d] - dates[d - 1]).days == 1)
                    and not (s["time"] == "NIGHT" and after and after["time"] == "DAY" and (dates[d + 1] - dates[d]).days == 1)):
                day["min"], day["time"] = day["min"] + m, s["time"]
                day["locs"].add(s["location"])
                day["ext"] += m if s["int_ext"] == "EXT" and s["time"] == "DAY" else 0
                plan[s["number"]] = d
                break
        else:
            return None
    return plan


def impossible(scenes, dates, unavailable):
    """Problems that make any schedule impossible, found before the solver is asked. -> list of strings"""
    out = []
    for a in sorted({a for s in scenes for a in s["cast"]}):
        if all(d.isoformat() in unavailable.get(a, ()) for d in dates):
            out.append(f"{a} has scenes but is unavailable on every shoot day")
    for s in scenes:
        if minutes(s) > MAX_MIN:
            out.append(f"scene {s['number']} alone needs {minutes(s)} minutes, more than one day")
    return out


def schedule(scenes, dates, rates, fees, unavailable, rain, previous=None, seconds=12.0, hint=None):
    """Assign every scene to a shoot day at the lowest total cost under the rules. `previous` {scene: day} is an earlier
    plan to stay close to. -> (plan {scene number: day index}, status, lower bound on the cost found)"""
    m, nd = cp_model.CpModel(), len(dates)
    x = {(s["number"], d): m.NewBoolVar("") for s in scenes for d in range(nd)}
    used, night = [m.NewBoolVar("") for _ in range(nd)], [m.NewBoolVar("") for _ in range(nd)]
    terms = []
    for s in scenes:
        m.AddExactlyOne(x[s["number"], d] for d in range(nd))
    cast = sorted({a for s in scenes for a in s["cast"]})
    locs = sorted({s["location"] for s in scenes})
    for d in range(nd):
        date = dates[d].isoformat()
        mins = sum(minutes(s) * x[s["number"], d] for s in scenes)
        m.Add(mins <= MAX_MIN * used[d])
        ot = m.NewIntVar(0, MAX_MIN - DAY_MIN, "")
        m.Add(ot >= mins - DAY_MIN)
        terms += [(CREW_DAY + 40 * d) * used[d], OT_PER_MIN * ot]          # the small extra per day index packs the schedule towards the start
        on = []
        for loc in locs:
            v = m.NewBoolVar("")
            for s in scenes:
                if s["location"] == loc:
                    m.AddImplication(x[s["number"], d], v)
            on.append(v)
            terms.append(fees[loc] * v)
        m.Add(sum(on) <= 2)
        move = m.NewBoolVar("")
        m.Add(move >= sum(on) - 1)
        terms.append(MOVE_COST * move)
        for s in scenes:
            m.AddImplication(x[s["number"], d], night[d] if s["time"] == "NIGHT" else night[d].Not())
            if any(date in unavailable.get(a, ()) for a in s["cast"]):
                m.Add(x[s["number"], d] == 0)
            if s["int_ext"] == "EXT" and rain.get(date):
                terms.append(round(rain[date] * minutes(s) * RAIN_PER_MIN) * x[s["number"], d])
            if previous and previous.get(s["number"]) is not None and previous[s["number"]] != d:
                terms.append(STAY_PUT * x[s["number"], d])
        m.Add(sum(minutes(s) * x[s["number"], d] for s in scenes if s["int_ext"] == "EXT" and s["time"] == "DAY") <= max(0, daylight(dates[d])[2] - 60))
        if d + 1 < nd and (dates[d + 1] - dates[d]).days == 1:
            m.Add(night[d] + used[d + 1] - night[d + 1] <= 1)        # no day call the morning after a night shoot
    for a in cast:
        work = []
        for d in range(nd):
            w = m.NewBoolVar("")
            for s in scenes:
                if a in s["cast"]:
                    m.AddImplication(x[s["number"], d], w)
            work.append(w)
        first, last = m.NewIntVar(0, nd - 1, ""), m.NewIntVar(0, nd - 1, "")
        for d in range(nd):
            m.Add(first <= d).OnlyEnforceIf(work[d])
            m.Add(last >= d).OnlyEnforceIf(work[d])
        m.Add(last >= first)
        terms.append(rates[a] * (last - first + 1))
    m.Minimize(sum(terms))
    for num, d in (hint or {}).items():
        for k in range(nd):
            m.AddHint(x[num, k], int(k == d))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 1
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, solver.StatusName(status), None
    return {s["number"]: next(d for d in range(nd) if solver.Value(x[s["number"], d])) for s in scenes}, solver.StatusName(status), solver.BestObjectiveBound()


# ---------------------------------------------------------------- call sheet

def call_sheet(scenes, plan, dates, day, rain):
    """Who is needed, where and when, for one shoot day. Scenes run grouped by location; calls are an hour before a
    performer's first scene."""
    ss = sorted((s for s in scenes if plan.get(s["number"]) == day), key=lambda s: (s["location"], s["number"]))
    if not ss:
        return None
    rise, set_, light = daylight(dates[day])
    night = ss[0]["time"] == "NIGHT"
    clock = (set_ + 0.5 if night else max(7.0, rise)) * 60        # crew call, minutes after midnight (local solar time)
    hhmm = lambda t: f"{int(t // 60) % 24:02d}:{int(t % 60):02d}"      # noqa: E731
    rows, calls, t = [], {}, clock + 60
    for s in ss:
        for a in s["cast"]:
            calls.setdefault(a, t - 60)
        rows.append({"scene": s["number"], "set": f"{s['int_ext']}. {s['location']} - {s['time']}", "pages": f"{s['eighths'] // 8} {s['eighths'] % 8}/8".replace(" 0/8", "").replace("0 ", ""), "cast": s["cast"], "starts": hhmm(t), "minutes": minutes(s)})
        t += minutes(s)
    total = sum(minutes(s) for s in ss)
    return {"date": dates[day].isoformat(), "day_number": sorted(set(plan.values())).index(day) + 1, "night_shoot": night, "crew_call": hhmm(clock), "estimated_wrap": hhmm(t), "shooting_minutes": total, "overtime_minutes": max(0, total - DAY_MIN),
            "locations": sorted({s["location"] for s in ss}), "company_move": len({s["location"] for s in ss}) > 1, "sunrise": hhmm(rise * 60), "sunset": hhmm(set_ * 60), "daylight_minutes": light,
            "rain_chance": rain.get(dates[day].isoformat(), 0), "exterior_work": any(s["int_ext"] == "EXT" for s in ss), "scenes": rows, "cast_calls": [{"cast": a, "call": hhmm(c)} for a, c in sorted(calls.items(), key=lambda kv: kv[1])],
            "times": "local solar time; a real call sheet would use clock time and the production's own turnaround rules"}
