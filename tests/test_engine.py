"""Unit and property tests for the parser, the rules, costing and the solver. No database."""
import datetime

import pytest

from fp import engine as e
from fp import world

SCRIPT = """FADE IN:

EXT. HARBOUR PIER - NIGHT

Mara waits by the rail. JONAH arrives late.

                    MARA
          You said Friday.

                    JONAH (CONT'D)
               (quietly)
          Lena knows nothing.

                    DR. OKAFOR (V.O.)
          It was never about the money.

                                             CUT TO:

INT. DINER - DAY

Young Mara counts the money twice.

                    YOUNG MARA
          Where is the key?

                    LENA (O.S.)
          Somewhere safe.

INT/EXT. CAR - DAY

FADE OUT.
"""
DATES = e.shoot_dates(datetime.date(2026, 11, 2), 6)
RATES, FEES = {"A": 1000, "B": 500, "C": 300}, {"X": 2000, "Y": 3000, "Z": 1000}
S = lambda n, loc, cast, eighths=8, ie="INT", time="DAY": {"number": n, "int_ext": ie, "location": loc, "time": time, "cast": cast, "eighths": eighths}      # noqa: E731


def test_parser_reads_headings_cast_voice_over_and_silent_characters():
    scenes, names, warnings = e.parse(SCRIPT)
    assert [(s["int_ext"], s["location"], s["time"]) for s in scenes] == [("EXT", "HARBOUR PIER", "NIGHT"), ("INT", "DINER", "DAY")]
    assert scenes[0]["cast"] == ["JONAH", "MARA"] and scenes[0]["voice_over"] == ["DR. OKAFOR"]            # Lena is only talked about; Okafor is only heard
    assert scenes[1]["cast"] == ["LENA", "YOUNG MARA"]                                                    # off-screen is still on set; "Young Mara" is not also "Mara"
    assert "CUT TO" not in " ".join(names) and len(warnings) == 1 and "INT/EXT. CAR - DAY" in warnings[0]
    assert e.parse_naive(SCRIPT)[0] == ["DR. OKAFOR", "JONAH", "MARA"]
    assert e.parse("no headings here") == ([], [], [])


@pytest.mark.parametrize("seed", [21, 77])
def test_parser_matches_the_generator_on_a_whole_screenplay(seed):
    text, truth = world.screenplay(seed)
    scenes, _, warnings = e.parse(text)
    assert not warnings and len(scenes) == len(truth) == 85
    assert all((s["int_ext"], s["location"], s["time"], s["cast"], s["eighths"]) == (x["int_ext"], x["location"], x["time"], sorted(x["cast"]), x["eighths"]) for s, x in zip(scenes, truth))


def test_minutes_and_daylight():
    assert e.minutes(S(1, "X", [], 8)) == 55 + 15 and e.minutes(S(1, "X", [], 16, "EXT")) == 140 + 15
    winter, summer = e.daylight(datetime.date(2026, 12, 21)), e.daylight(datetime.date(2026, 6, 21))
    assert 575 < winter[2] < 600 and 845 < summer[2] < 875 and abs(winter[0] + winter[1] - 24) < 1e-9       # geometric day length at 34 degrees north: a few minutes short of almanac values, which add refraction
    assert [d.weekday() for d in DATES] == [0, 1, 2, 3, 4, 0]


def test_costing_pays_cast_from_first_to_last_day():
    scenes = [S(1, "X", ["A"]), S(2, "Y", ["A", "B"]), S(3, "X", ["B"], 96)]
    c = e.cost(scenes, {1: 0, 2: 3, 3: 3}, DATES, RATES, FEES, {})
    assert c["cast"] == 1000 * 4 + 500 * 1 and c["cast_work_days"]["A"] == 2 and c["cast_span_days"]["A"] == 4      # A is paid for two idle days in between
    assert c["crew"] == 2 * e.CREW_DAY and c["locations"] == 2000 + 3000 + 2000 and c["company_moves"] == e.MOVE_COST
    minutes = e.minutes(scenes[1]) + e.minutes(scenes[2])
    assert c["overtime"] == e.OT_PER_MIN * (minutes - e.DAY_MIN) and c["overtime_days"] == [{"date": "2026-11-05", "minutes_over": minutes - e.DAY_MIN}]
    wet = e.cost([S(1, "X", ["A"], 8, "EXT")], {1: 0}, DATES, RATES, FEES, {"2026-11-02": 0.5})
    assert wet["weather_risk"] == round(0.5 * 85 * e.RAIN_PER_MIN)


@pytest.mark.parametrize("plan,scenes,unavailable,expected", [
    ({1: 0, 2: 0}, [S(1, "X", ["A"]), S(2, "Y", ["B"])], {}, []),
    ({1: 0}, [S(1, "X", ["A"]), S(2, "Y", ["B"])], {}, ["scene 2 is not scheduled"]),
    ({1: 0, 2: 0, 3: 0}, [S(1, "X", []), S(2, "Y", []), S(3, "Z", [])], {}, ["more than two locations"]),
    ({1: 0, 2: 0}, [S(1, "X", []), S(2, "X", [], time="NIGHT")], {}, ["day and night scenes on the same call"]),
    ({1: 0, 2: 1}, [S(1, "X", [], time="NIGHT"), S(2, "X", [])], {}, ["a day call follows a night shoot"]),
    ({1: 4, 2: 5}, [S(1, "X", [], time="NIGHT"), S(2, "X", [])], {}, []),                                   # Friday night, Monday day: the weekend is the turnaround
    ({1: 2}, [S(1, "X", ["A"])], {"A": ["2026-11-04"]}, ["A is not available"]),
    ({1: 0}, [S(1, "X", [], 100, "EXT")], {}, ["exceeds the 720-minute day", "minutes of daylight"]),
])
def test_rule_checker(plan, scenes, unavailable, expected):
    got = e.violations(scenes, plan, DATES, unavailable)
    assert len(got) == len(expected) and all(x in g for x, g in zip(expected, got))


def test_solver_respects_every_rule_and_beats_script_order_on_a_small_film():
    text, _ = world.screenplay(5, 30)
    scenes, _, _ = e.parse(text)
    dates = e.shoot_dates(datetime.date(2026, 11, 2), 12)
    un = {"MARA": [dates[1].isoformat(), dates[2].isoformat()]}
    base = e.script_order(scenes, dates, un)
    plan, status, bound = e.schedule(scenes, dates, world.RATES, world.FEES, un, {}, hint=base, seconds=6)
    assert status in ("OPTIMAL", "FEASIBLE") and e.violations(scenes, plan, dates, un) == [] == e.violations(scenes, base, dates, un)
    a, b = e.cost(scenes, plan, dates, world.RATES, world.FEES, {}), e.cost(scenes, base, dates, world.RATES, world.FEES, {})
    assert bound <= a["total"] + 40 * 12 * 12 and a["total"] < b["total"]
    rain = {dates[0].isoformat(): 0.9}
    wet, _, _ = e.schedule(scenes, dates, world.RATES, world.FEES, un, rain, previous=plan, seconds=6)
    ext_on_rain = lambda p: sum(1 for s in scenes if s["int_ext"] == "EXT" and p[s["number"]] == 0)      # noqa: E731
    assert e.violations(scenes, wet, dates, un) == [] and ext_on_rain(wet) <= ext_on_rain(plan)
    assert sum(plan[k] != wet[k] for k in plan) < len(scenes) * 0.6                                         # stays close to the earlier plan


def test_impossible_requests_are_caught_before_solving():
    scenes = [S(1, "X", ["A"]), S(2, "X", ["B"], 200)]
    assert e.impossible(scenes, DATES, {"A": [d.isoformat() for d in DATES]}) == ["A has scenes but is unavailable on every shoot day", "scene 2 alone needs 1390 minutes, more than one day"]
    assert e.impossible(scenes[:1], DATES, {"A": [DATES[0].isoformat()]}) == []
    assert e.script_order([S(1, "X", ["A"])], DATES[:1], {"A": [DATES[0].isoformat()]}) is None


def test_call_sheet():
    scenes = [S(1, "X", ["A"], 8), S(2, "Y", ["B"], 16), S(3, "X", ["A", "C"], 8)]
    sheet = e.call_sheet(scenes, {1: 2, 2: 2, 3: 2}, DATES, 2, {"2026-11-04": 0.4})
    assert [r["scene"] for r in sheet["scenes"]] == [1, 3, 2] and sheet["company_move"] and sheet["day_number"] == 1 and sheet["rain_chance"] == 0.4      # grouped by location
    assert sheet["crew_call"] == "07:00" and sheet["scenes"][0]["starts"] == "08:00" and sheet["cast_calls"][0] == {"cast": "A", "call": "07:00"} and sheet["cast_calls"][-1]["cast"] == "B"
    assert sheet["shooting_minutes"] == 70 + 70 + 125 and e.call_sheet(scenes, {1: 2, 2: 2, 3: 2}, DATES, 0, {}) is None
    night = e.call_sheet([S(1, "X", ["A"], time="NIGHT")], {1: 0}, DATES, 0, {})
    assert night["night_shoot"] and night["crew_call"] > night["sunset"]
