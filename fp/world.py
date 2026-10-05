"""A deterministic synthetic screenplay in standard format, with the truth it was written from (who is on set in each
scene, where, day or night, how long). The story is nonsense assembled from sentence banks: what matters is that the
format is real (sluglines, character cues, voice-over, continued dialogue), so the parser can be scored."""
import functools
import random

CAST = ["MARA", "JONAH", "DR. OKAFOR", "INSPECTOR HALE", "LENA", "TOMAS", "MRS. ABERNATHY", "KWAME", "SOFIA", "THE BROKER", "YOUNG MARA", "RADIO HOST"]
WEIGHT = [0.62, 0.45, 0.22, 0.2, 0.2, 0.14, 0.1, 0.1, 0.09, 0.07, 0.05, 0.03]              # how often each is in a scene
# name, INT or EXT, share of its scenes at night, daily fee
LOCATIONS = [("HARBOUR OFFICE", "INT", 0.2, 3500), ("MARA'S APARTMENT", "INT", 0.5, 2500), ("POLICE STATION", "INT", 0.3, 4000), ("HOSPITAL CORRIDOR", "INT", 0.4, 6000), ("DINER", "INT", 0.5, 3000),
             ("WAREHOUSE", "INT", 0.6, 4500), ("ABERNATHY HOUSE - KITCHEN", "INT", 0.2, 2800), ("RADIO STUDIO", "INT", 0.7, 2200), ("FISH MARKET", "EXT", 0.1, 5000), ("HARBOUR PIER", "EXT", 0.4, 7000),
             ("CLIFF ROAD", "EXT", 0.3, 8000), ("ROOFTOP", "EXT", 0.6, 5500), ("TRAIN PLATFORM", "EXT", 0.3, 7500), ("SCHOOLYARD", "EXT", 0.0, 3000)]
ACTION = ["{a} crosses to the window and watches the street.", "{a} sets the folder down. Nobody speaks.", "{a} checks a watch, then the door.", "A phone rings. {a} lets it ring.", "{a} pours two cups and slides one across.",
          "Rain against the glass. {a} pulls a coat tighter.", "{a} counts the money twice.", "{a} finds the photograph and turns it over.", "The lights flicker. {a} does not look up.", "{a} follows at a distance.",
          "A car idles outside. {a} notices.", "{a} unfolds the map across the table.", "{a} waits until the footsteps fade."]
LINES = ["You said it would be finished by Friday.", "I never said that.", "Then who signed the ledger?", "Ask Jonah. He was there.", "We do not have time for this.", "Tell me what you saw on the pier.",
         "Nothing. It was dark.", "That is not what you told the inspector.", "I was frightened.", "You should be.", "Where is the key?", "Somewhere safe.", "Safe from whom?", "From Mara, mostly.",
         "Sit down.", "I would rather stand.", "It was never about the money.", "It is always about the money.", "Call me when the boat comes in.", "And if it does not?", "Then do not call."]
TRANSITIONS = ["CUT TO:", "DISSOLVE TO:", "SMASH CUT TO:"]
LINES_PER_PAGE = 55


def pick(r, k):
    return r.choices(range(len(CAST)), WEIGHT, k=k)


@functools.lru_cache(maxsize=4)
def screenplay(seed=21, n_scenes=85):
    """-> (text, scenes) where scenes is the truth: [{number, int_ext, location, time, cast: [names on set], eighths}]"""
    r = random.Random(seed)
    out, scenes = ["FADE IN:", ""], []
    for n in range(1, n_scenes + 1):
        name, ie, night, _ = r.choices(LOCATIONS, [9, 8, 6, 4, 6, 5, 4, 3, 4, 6, 4, 4, 3, 2])[0]
        time = "NIGHT" if r.random() < night else "DAY"
        cast = sorted(set(pick(r, r.choice([1, 2, 2, 3, 3, 4, 5]))))
        absent = [k for k in range(len(CAST)) if k not in cast]
        target = r.choice([3, 4, 6, 8, 8, 10, 12, 14, 18, 22, 28]) * LINES_PER_PAGE // 8         # lines: three eighths of a page up to three and a half pages
        body, seen = [f"{ie}. {name} - {time}", ""], set()
        while len(body) < target:
            kind = r.random()
            if kind < 0.33:
                k = r.choice(cast)
                who = CAST[k]
                seen.add(k)
                body += [r.choice(ACTION).format(a=who.title() if r.random() < 0.7 else who), ""]          # names in action lines are mostly not in capitals
            elif kind < 0.93:
                k = r.choice(cast)
                seen.add(k)
                cue = CAST[k] + r.choice(["", "", "", " (CONT'D)", " (O.S.)"])
                body += [" " * 20 + cue, " " * 10 + r.choice(LINES), ""]
            elif kind < 0.945 and absent:
                body += [" " * 20 + CAST[r.choice(absent)] + " (V.O.)", " " * 10 + r.choice(LINES), ""]      # heard, not on set
            else:
                k = r.choice(cast)
                seen.add(k)
                body += [" " * 20 + CAST[k], " " * 15 + "(quietly)", " " * 10 + f"{CAST[r.choice(absent)].title() if absent else 'Nobody'} knows. " + r.choice(LINES), ""]     # a name spoken in dialogue is not a person on set
        if r.random() < 0.3:
            body += [" " * 45 + r.choice(TRANSITIONS), ""]
        scenes.append({"number": n, "int_ext": ie, "location": name, "time": time, "cast": [CAST[k] for k in sorted(seen)], "eighths": max(1, round(len(body) * 8 / LINES_PER_PAGE))})
        out += body
    return "\n".join(out + ["FADE OUT."]), scenes


RATES = {"MARA": 6500, "JONAH": 5000, "DR. OKAFOR": 2600, "INSPECTOR HALE": 2600, "LENA": 2200, "TOMAS": 1800, "MRS. ABERNATHY": 1800, "KWAME": 1500, "SOFIA": 1500, "THE BROKER": 2000, "YOUNG MARA": 1200, "RADIO HOST": 1200}
FEES = {name: fee for name, _, _, fee in LOCATIONS}
