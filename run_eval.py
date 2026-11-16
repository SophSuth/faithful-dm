#!/usr/bin/env python3
"""
The Faithful DM: runner and scorer. Full character sheets only.
Standard library only, no API keys needed.

Every model gets the character's complete clean sheet (<name>-clean.json)
with the scenario's starting state merged in, plus one event, and must
return the whole sheet updated.

    python3 run_eval.py --demo                    check the scorer, no models needed
    python3 run_eval.py --prompt S-05             print one prompt to paste into a chat
    python3 run_eval.py --all-prompts             write all_prompts.txt (every scenario)
    python3 run_eval.py --add S-05 "Model Name"   save reply.txt as a result
    python3 run_eval.py --score results_full.jsonl

After editing my_scenarios.txt, run make_scenarios.py first, then
--all-prompts, so the prompt file matches the scenarios.

Scoring, three separate numbers
-------------------------------
State, decides PASS or FAIL:
  * Every check in the scenario must match the expected sheet. The checks
    are the fields named in the scenario's expected state.
  * The reply may not contain fields the expected sheet does not have.
    An invented field, or an action_result on an action that worked, fails.
  * Conditions compare as sets with implied conditions expanded. In the
    2024 rules Unconscious includes Incapacitated and Prone, so
    ["unconscious"] and ["unconscious", "prone"] are the same answer.
  * true is not 1. Whole numbers may be written as 27 or 27.0.

Preservation, reported as a rate:
  * Every other field on the sheet should come back unchanged. A slip
    here (say, rewriting a spell's school) is listed and counted but does
    not fail the scenario. Long prose is skipped: fields named summary,
    description, source or scale_type, and strings over 80 characters.

Format, reported as a count:
  * A bare JSON object is format ok. Code fences or a sentence around the
    JSON are a format slip. The JSON is still pulled out and scored.

Prompt versions:
  * Each saved result carries prompt_sha, a fingerprint of the exact
    prompt the model saw. If the scenario or the sheet changes later, the
    result is reported as STALE and left out of the totals.

Building the sheet from a scenario:
  * Nested objects merge field by field.
  * class keeps the sheet's full text ("Wizard 3 (Evoker)").
  * inventory merges item by item on the name, and only the scenario's
    items are part of the state check.
  * Use counts are synced from feature_uses into the features list, so the
    sheet never contradicts itself.

Note: sheets include feature summaries, which sometimes state the rule
being tested (Rage says it regains one use on a Short Rest), and derived
numbers like saving throw totals. That is realistic for a DM reading a
sheet, and it is part of what this benchmark measures.
"""

import argparse
import copy
import difflib
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SCENARIOS = HERE / "my_scenarios.jsonl"
RESULTS_FILE = HERE / "results_full.jsonl"
REPLY_FILE = HERE / "reply.txt"
PROMPTS_FILE = HERE / "all_prompts.txt"

SYSTEM_PROMPT = (
    "You are the state tracker for a Dungeons & Dragons 5e game using the 2024 revised rules (SRD 5.2). "
    "You will be given a starting character state as JSON and one game event. "
    "All dice results you need are given in the event. Do not roll dice. "
    "Do not invent rules, items, features, or resources that are not in the starting state. "
    "Return only the updated state as a single JSON object with exactly the same field names "
    "as the starting state. If the event includes an action that cannot be done with the "
    "resources in the starting state, leave the state unchanged and add a field called "
    "action_result with the value failed_no_slot for a spell with no slot, or "
    "failed_impossible for any other impossible action. "
    "Add no narration, no explanation, and no markdown fences."
)

IMPLIED_CONDITIONS = {
    "unconscious": {"incapacitated", "prone"},
    "paralyzed": {"incapacitated"},
    "petrified": {"incapacitated"},
    "stunned": {"incapacitated"},
}
SET_KEYS = {"resistances", "immunities", "vulnerabilities"}  # order and case don't matter
SKIP_KEYS = {"summary", "description", "source", "scale_type"}
MAX_STRING = 80
KEEP_SHEET_KEYS = {"class"}
MERGE_BY_NAME_KEYS = {"inventory"}


# ---------------------------------------------------------------- loading

def load_scenarios(path=None):
    scenarios = {}
    for line in Path(path or SCENARIOS).read_text().splitlines():
        if line.strip():
            s = json.loads(line)
            scenarios[s["id"]] = s
    return scenarios


def load_results(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- building sheets

def merge_by_name(base_list, override_list):
    out = copy.deepcopy(base_list)
    for item in override_list:
        match = next((i for i, b in enumerate(out)
                      if isinstance(b, dict) and b.get("name") == item.get("name")), None)
        if match is None:
            out.append(copy.deepcopy(item))
        else:
            out[match] = {**out[match], **copy.deepcopy(item)}
    return out


def deep_merge(base, override, top=True):
    out = copy.deepcopy(base)
    for key, value in override.items():
        if top and key in KEEP_SHEET_KEYS and key in out:
            continue
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value, top=False)
        elif (top and key in MERGE_BY_NAME_KEYS and isinstance(out.get(key), list)
              and isinstance(value, list)):
            out[key] = merge_by_name(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def sync_feature_uses(state):
    """feature_uses is the source of truth for use counts."""
    uses, features = state.get("feature_uses"), state.get("features")
    if isinstance(uses, dict) and isinstance(features, list):
        for feature in features:
            if isinstance(feature, dict) and feature.get("name") in uses:
                for key in ("uses_max", "uses_remaining"):
                    if key in uses[feature["name"]]:
                        feature[key] = uses[feature["name"]][key]
    return state


def build_states(scenario):
    sheet = json.loads((HERE / f"{scenario['character'].lower()}-clean.json").read_text())
    start = sync_feature_uses(deep_merge(sheet, scenario["starting_state"]))
    expected = sync_feature_uses(deep_merge(sheet, scenario["expected_state"]))
    return start, expected


# ---------------------------------------------------------------- prompts

def user_prompt(scenario):
    start, _ = build_states(scenario)
    return ("Starting state:\n" + json.dumps(start)
            + "\n\nEvent:\n" + scenario["event"]
            + "\n\nReturn only the updated state JSON.")


def full_prompt_text(scenario):
    return "SYSTEM:\n" + SYSTEM_PROMPT + "\n\nUSER:\n" + user_prompt(scenario)


def prompt_sha(scenario):
    """Short fingerprint of exactly what the model is shown."""
    return hashlib.sha256(full_prompt_text(scenario).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- parsing

def parse_reply(reply):
    """Return (parsed_dict_or_None, format_ok)."""
    if isinstance(reply, dict):
        return reply, True
    text = str(reply).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, True
    except json.JSONDecodeError:
        pass
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.I)
    if fenced:
        candidates.append(fenced.group(1).strip())
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, False
        except json.JSONDecodeError:
            continue
    return None, False


def describe_unparsed(reply):
    """Say why a reply could not be read, so a cut-off paste is obvious."""
    text = str(reply).strip()
    body = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    if body.startswith("{"):
        depth, in_string, escaped = 0, False, False
        for ch in body:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        if depth > 0:
            return (f"truncated: the reply starts as JSON but is cut off after {len(text)} "
                    "characters. Check the copy and paste, or whether the model stopped early. "
                    "This is usually a capture problem, not a state answer.")
        return "invalid_json: the reply looks like JSON but does not parse"
    return "no_json: no JSON object could be found in the reply"


# ---------------------------------------------------------------- comparing

def get_field(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None, False
    return cur, True


def leaf_paths(obj, prefix=""):
    """Leaf paths through nested dicts. Lists and scalars are leaves."""
    paths = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict) and value:
                paths.extend(leaf_paths(value, path + "."))
            else:
                paths.append(path)
    return paths


def expand_conditions(value):
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        return None
    out = set()
    for condition in value:
        name = condition.strip().lower()
        out.add(name)
        out |= IMPLIED_CONDITIONS.get(name, set())
    return out


def values_equal(expected, actual, key=None):
    if key == "conditions":
        exp, act = expand_conditions(expected), expand_conditions(actual)
        if exp is not None and act is not None:
            return exp == act
    if key in SET_KEYS and isinstance(expected, list) and isinstance(actual, list):
        if all(isinstance(v, str) for v in expected + actual):
            return {v.strip().lower() for v in expected} == {v.strip().lower() for v in actual}
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    if isinstance(expected, dict) and isinstance(actual, dict):
        return (expected.keys() == actual.keys()
                and all(values_equal(expected[k], actual[k], k) for k in expected))
    if isinstance(expected, list) and isinstance(actual, list):
        return (len(expected) == len(actual)
                and all(values_equal(e, a) for e, a in zip(expected, actual)))
    return type(expected) is type(actual) and expected == actual


def extra_fields(expected, parsed):
    extras = []
    for path in leaf_paths(parsed):
        _, found = get_field(expected, path)
        if not found:
            parent = path.rsplit(".", 1)[0] if "." in path else None
            parent_value, parent_found = get_field(expected, parent) if parent else (None, False)
            if parent_found and not isinstance(parent_value, dict):
                continue
            extras.append(path)
    return extras


def accepted(scenario, path, want, got):
    """Matches the expected value, or one of the scenario's Also accept answers."""
    key = path.rsplit(".", 1)[-1]
    if values_equal(want, got, key):
        return True
    return any(values_equal(alt, got, key)
               for alt in scenario.get("also_accept", {}).get(path, []))


def check_named_items(want_items, got_list):
    if not isinstance(got_list, list):
        return False, f"expected a list, got {got_list!r}"
    for want in want_items:
        got = next((g for g in got_list if isinstance(g, dict) and g.get("name") == want["name"]), None)
        if got is None or not values_equal(want, {k: got.get(k) for k in want}):
            return False, f"expected {want!r}, got {got!r}"
    return True, ""


def state_check(scenario, expected, parsed):
    failures, passed = [], 0
    for check in scenario["checks"]:
        if check in MERGE_BY_NAME_KEYS:
            ok, detail = check_named_items(scenario["expected_state"][check], parsed.get(check))
        else:
            want, _ = get_field(expected, check)
            got, found = get_field(parsed, check)
            ok = found and accepted(scenario, check, want, got)
            detail = (f"expected {want!r}, got {got!r}" if found
                      else f"expected {want!r}, got MISSING")
        if ok:
            passed += 1
        else:
            failures.append(f"{check}: {detail}")
    for path in extra_fields(expected, parsed):
        if added_but_accepted(scenario, expected, parsed, path):
            continue
        value, _ = get_field(parsed, path)
        failures.append(f"{path}: not in the expected sheet, reply added {value!r}")
    return passed, len(scenario["checks"]), failures


def added_but_accepted(scenario, expected, parsed, path):
    """A new field is fine when it sits inside a field whose whole value
    matches one of the scenario's Also accept answers (e.g. tremorsense
    added to senses in S-18)."""
    for accept_path in scenario.get("also_accept", {}):
        if path.startswith(accept_path + "."):
            want, _ = get_field(expected, accept_path)
            got, found = get_field(parsed, accept_path)
            if found and accepted(scenario, accept_path, want, got):
                return True
    return False


def scored_leaves(obj, prefix=""):
    """(path, value) for every leaf, walking into lists, skipping prose."""
    if isinstance(obj, dict):
        items = obj.items()
    elif isinstance(obj, list):
        items = ((str(i), v) for i, v in enumerate(obj))
    else:
        return
    for key, value in items:
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, (dict, list)) and value:
            yield from scored_leaves(value, path)
        elif key in SKIP_KEYS or (isinstance(value, str) and len(value) > MAX_STRING):
            continue
        else:
            yield path, value


def preservation_check(scenario, expected, parsed):
    """Compare every field the state check doesn't cover.

    Lists are lined up item by item (like a diff), not by position, so
    one inserted or dropped item counts as one slip instead of shifting
    every item after it.
    """
    checks = scenario["checks"]
    # A field with Also accept answers is compared as a whole, so its
    # alternatives can apply, instead of item by item.
    whole = [p for p in scenario.get("also_accept", {}) if p not in checks]
    skip = checks + whole

    def covered(path):
        return any(path == c or path.startswith(c + ".") for c in skip)

    kept, total, slips = compare_kept(scenario, expected, parsed, "", covered)
    for path in whole:
        want, found_want = get_field(expected, path)
        if not found_want:
            continue
        got, found = get_field(parsed, path)
        total += 1
        if found and accepted(scenario, path, want, got):
            kept += 1
        else:
            slips.append(f"{path}: expected {want!r}, got {got!r}" if found
                         else f"{path}: expected {want!r}, got MISSING")
    return kept, total, slips


def count_leaves(obj, path, covered):
    """How many scored leaves a subtree holds (same skip rules as below)."""
    if covered(path):
        return 0
    if isinstance(obj, dict) and obj:
        return sum(count_leaves(v, f"{path}.{k}" if path else k, covered)
                   for k, v in obj.items()
                   if not (k in SKIP_KEYS and not isinstance(v, (dict, list))))
    if isinstance(obj, list) and obj:
        return sum(count_leaves(v, f"{path}.{i}", covered) for i, v in enumerate(obj))
    if isinstance(obj, str) and len(obj) > MAX_STRING:
        return 0
    return 1


def short(value, limit=80):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...}"


def compare_kept(scenario, want, got, path, covered):
    """Return (kept, total, slips) for one subtree of the expected sheet."""
    if covered(path):
        return 0, 0, []
    if isinstance(want, dict) and want:
        if not isinstance(got, dict):
            n = count_leaves(want, path, covered)
            return 0, n, [f"{path}: expected an object, got {short(got)}"] if n else []
        kept = total = 0
        slips = []
        for key, value in want.items():
            if key in SKIP_KEYS and not isinstance(value, (dict, list)):
                continue
            sub = f"{path}.{key}" if path else key
            if key in got:
                k, n, s = compare_kept(scenario, value, got[key], sub, covered)
            else:
                n = count_leaves(value, sub, covered)
                k, s = 0, ([f"{sub}: expected {short(value)}, got MISSING"] if n else [])
            kept, total, slips = kept + k, total + n, slips + s
        return kept, total, slips
    if isinstance(want, list) and want:
        if not isinstance(got, list):
            n = count_leaves(want, path, covered)
            return 0, n, [f"{path}: expected a list, got {short(got)}"] if n else []
        key = lambda item: json.dumps(item, sort_keys=True)  # noqa: E731
        matcher = difflib.SequenceMatcher(None, [key(x) for x in want], [key(x) for x in got],
                                          autojunk=False)
        kept = total = 0
        slips = []
        dropped, added = [], []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            pairs = list(zip(range(i1, i2), range(j1, j2))) if tag in ("equal", "replace") else []
            for i, j in pairs:
                k, n, s = compare_kept(scenario, want[i], got[j], f"{path}.{i}", covered)
                kept, total, slips = kept + k, total + n, slips + s
            dropped += range(i1 + len(pairs), i2)
            added += range(j1 + len(pairs), j2)
        # An item that only moved is still there, unchanged: not a slip.
        for i in list(dropped):
            j = next((j for j in added if key(got[j]) == key(want[i])), None)
            if j is not None:
                n = count_leaves(want[i], f"{path}.{i}", covered)
                kept, total = kept + n, total + n
                dropped.remove(i)
                added.remove(j)
        for i in dropped:
            n = count_leaves(want[i], f"{path}.{i}", covered)
            total += n
            slips.append(f"{path}.{i}: reply dropped {short(want[i])}")
        for j in added:
            total += 1
            slips.append(f"{path}: reply added an extra item {short(got[j])}")
        return kept, total, slips
    if isinstance(want, str) and len(want) > MAX_STRING:
        return 0, 0, []
    if values_equal(want, got, path.rsplit(".", 1)[-1]) or accepted(scenario, path, want, got):
        return 1, 1, []
    return 0, 1, [f"{path}: expected {want!r}, got {got!r}"]


def score_reply(scenario, reply):
    """Everything about one reply, as a dict."""
    _, expected = build_states(scenario)
    parsed, format_ok = parse_reply(reply)
    if parsed is None:
        return {"ok": False, "format_ok": False, "passed": 0, "total": len(scenario["checks"]),
                "failures": [describe_unparsed(reply)],
                "kept": 0, "fields": 0, "slips": []}
    passed, total, failures = state_check(scenario, expected, parsed)
    kept, fields, slips = preservation_check(scenario, expected, parsed)
    return {"ok": not failures, "format_ok": format_ok, "passed": passed, "total": total,
            "failures": failures, "kept": kept, "fields": fields, "slips": slips}


# ---------------------------------------------------------------- commands

def score_file(path, scenarios):
    tally = defaultdict(lambda: {"pass": 0, "n": 0, "format_slips": 0, "kept": 0,
                                 "fields": 0, "legacy": 0, "stale": 0})
    for row in load_results(path):
        scenario = scenarios.get(row["id"])
        if scenario is None:
            print(f"Unknown scenario id: {row['id']}, skipped")
            continue
        model = row.get("model", "model")
        t = tally[model]
        stored = row.get("prompt_sha")
        if stored is not None and stored != prompt_sha(scenario):
            t["stale"] += 1
            print(f"{row['id']} [{model}]: STALE, the scenario or sheet changed after this "
                  "reply was saved. Rerun it. Not counted.")
            continue
        r = score_reply(scenario, row["reply"])
        t["n"] += 1
        t["pass"] += r["ok"]
        t["format_slips"] += not r["format_ok"]
        t["legacy"] += stored is None
        t["kept"] += r["kept"]
        t["fields"] += r["fields"]
        notes = (["format slip"] if not r["format_ok"] else []) + \
                (["legacy, no prompt fingerprint"] if stored is None else [])
        note = f"  ({', '.join(notes)})" if notes else ""
        print(f"{row['id']} [{model}]: state {r['passed']}/{r['total']} checks, "
              f"{'PASS' if r['ok'] else 'FAIL'}; kept {r['kept']}/{r['fields']} other fields{note}")
        for failure in r["failures"]:
            print(f"    - {failure}")
        for slip in r["slips"][:10]:
            print(f"    ~ copy slip {slip}")
        if len(r["slips"]) > 10:
            print(f"    ~ ... and {len(r['slips']) - 10} more copy slips")
    print("\nTotals per model (stale results left out):")
    for model, t in tally.items():
        rate = f"{100 * t['kept'] / t['fields']:.1f}%" if t["fields"] else "n/a"
        print(f"  {model}: {t['pass']}/{t['n']} scenarios passed, {rate} of other fields kept, "
              f"{t['format_slips']} format slip(s)"
              + (f", {t['legacy']} legacy" if t["legacy"] else "")
              + (f", {t['stale']} stale skipped" if t["stale"] else ""))


def add_result(scenario_id, model, scenarios):
    """Append reply.txt to results_full.jsonl, verbatim, with its fingerprint."""
    if scenario_id not in scenarios:
        print(f"{scenario_id} is not an ID in my_scenarios.jsonl. Nothing saved.")
        return 1
    if not REPLY_FILE.exists() or not REPLY_FILE.read_text().strip():
        print("reply.txt is missing or empty. Paste the model's reply into "
              "reply.txt, save it, then run this again.")
        return 1
    reply = REPLY_FILE.read_text().strip()
    sha = prompt_sha(scenarios[scenario_id])
    done = load_results(RESULTS_FILE) if RESULTS_FILE.exists() else []
    if any(r["id"] == scenario_id and r["model"] == model
           and r.get("prompt_sha") in (None, sha) for r in done):
        print(f"{scenario_id} for {model} is already saved for this version of the scenario. "
              "Not adding a duplicate. If the model answered again, that is a second "
              "attempt and should not replace the first.")
        return 1
    if any(r["reply"] == reply for r in done):
        print("Warning: this exact reply is already saved for another scenario or model. "
              "Check reply.txt holds the new answer. Saving anyway.")
    with RESULTS_FILE.open("a") as f:
        f.write(json.dumps({"id": scenario_id, "model": model, "reply": reply,
                            "prompt_sha": sha}) + "\n")
    REPLY_FILE.write_text("")
    print(f"Saved {scenario_id} for {model}. {len(done) + 1} result(s) in "
          f"{RESULTS_FILE.name}. reply.txt is now empty and ready for the next one.")
    return 0


def write_all_prompts(scenarios):
    blocks = [f"===== {sid} =====\n" + full_prompt_text(s) for sid, s in scenarios.items()]
    PROMPTS_FILE.write_text("\n\n".join(blocks) + "\n")
    print(f"Wrote {PROMPTS_FILE.name} with {len(blocks)} full sheet prompts.")


def run_demo(scenarios):
    ok_all = True

    def expect(label, r, want_ok, want_format_ok=True, want_slips=0):
        nonlocal ok_all
        good = (r["ok"] == want_ok and r["format_ok"] == want_format_ok
                and len(r["slips"]) == want_slips)
        ok_all &= good
        print(f"  {'ok ' if good else 'BAD'} {label}: {'PASS' if r['ok'] else 'FAIL'}, "
              f"format {'ok' if r['format_ok'] else 'slip'}, {len(r['slips'])} copy slip(s)")
        for f in r["failures"] + r["slips"]:
            print(f"        - {f}")

    def sheet(sid, **changes):
        _, expected = build_states(scenarios[sid])
        expected.update(changes)
        return expected

    print("1. The answer key itself passes every scenario")
    for sid, s in scenarios.items():
        r = score_reply(s, json.dumps(build_states(s)[1]))
        if not r["ok"] or r["slips"]:
            expect(sid, r, True)
    print(f"  checked {len(scenarios)} scenarios")

    print("2. Wrong state fails")
    expect("S-04 ignoring resistance", score_reply(scenarios["S-04"], json.dumps(sheet("S-04", hp_current=15))), False)

    print("3. Invented field fails")
    expect("S-06 with a stray action_result",
           score_reply(scenarios["S-06"], json.dumps(sheet("S-06", action_result="failed_no_slot"))), False)

    print("4. Implied conditions are accepted")
    expect("S-17 listing prone too",
           score_reply(scenarios["S-17"], json.dumps(sheet("S-17", conditions=["prone", "unconscious"]))), True)
    expect("S-17 prone without unconscious",
           score_reply(scenarios["S-17"], json.dumps(sheet("S-17", conditions=["prone"]))), False)

    print("5. true is not 1")
    expect("S-01 dead written as 1", score_reply(scenarios["S-01"], json.dumps(sheet("S-01", dead=1))), False)

    print("6. Format slips are counted but do not fail the state")
    body = json.dumps(sheet("S-02"))
    expect("S-02 in a code fence", score_reply(scenarios["S-02"], f"```json\n{body}\n```"), True, False)
    expect("S-02 with a sentence first", score_reply(scenarios["S-02"], "Here you go:\n" + body), True, False)
    expect("S-02 with no JSON at all", score_reply(scenarios["S-02"], "Jimmy is at 15 HP."), False, False)

    print("7. Copy slips are counted but do not fail the state")
    slipped = sheet("S-07")
    slipped["spell_details"] = copy.deepcopy(slipped["spell_details"])
    slipped["spell_details"][0]["school"] = "Necromancy"
    expect("S-07 with one spell school rewritten", score_reply(scenarios["S-07"], json.dumps(slipped)),
           True, True, want_slips=1)
    moved = sheet("S-07")
    moved["languages"] = list(reversed(moved["languages"]))
    expect("S-07 with languages reordered (not a slip)", score_reply(scenarios["S-07"], json.dumps(moved)), True)
    extra = sheet("S-08")
    extra["inventory"] = extra["inventory"][:5] + [{"name": "Dagger", "quantity": 1, "equipped": False}] + extra["inventory"][5:]
    expect("S-08 with one extra Dagger (one slip, not a cascade)",
           score_reply(scenarios["S-08"], json.dumps(extra)), True, want_slips=1)

    print("8. Inventory: only the scenario's item is a state check")
    expect("S-20 potion still at 2", score_reply(scenarios["S-20"], json.dumps(build_states(scenarios["S-20"])[0]
                                                                                | {"hp_current": 24})), False)

    print("9. Also accept answers count, anything else still fails")
    expect("S-01 dead with conditions emptied",
           score_reply(scenarios["S-01"], json.dumps(sheet("S-01", conditions=[]))), True)
    expect("S-03 raging with resistances written out",
           score_reply(scenarios["S-03"], json.dumps(sheet("S-03", resistances=["Slashing", "poison", "Piercing", "Bludgeoning"]))), True)
    expect("S-03 with a made-up resistance",
           score_reply(scenarios["S-03"], json.dumps(sheet("S-03", resistances=["poison", "fire"]))),
           True, want_slips=1)

    expect("S-18 with tremorsense added to senses",
           score_reply(scenarios["S-18"], json.dumps(sheet("S-18", senses={"darkvision": 120, "tremorsense": 60}))), True)
    expect("S-18 with a made-up sense",
           score_reply(scenarios["S-18"], json.dumps(sheet("S-18", senses={"darkvision": 120, "blindsight": 60}))), False, want_slips=1)

    print("10. A cut-off reply is labelled as truncated")
    cut = score_reply(scenarios["S-05"], json.dumps(sheet("S-05"))[:2000])
    ok_all &= cut["failures"][0].startswith("truncated")
    print(f"  {'ok ' if cut['failures'][0].startswith('truncated') else 'BAD'} {cut['failures'][0][:60]}...")

    print("\nScorer works." if ok_all else "\nSomething is off: a line above is marked BAD.")
    return 0 if ok_all else 1


def main():
    parser = argparse.ArgumentParser(description="The Faithful DM, full character sheets")
    parser.add_argument("--demo", action="store_true", help="Check the scorer, no models needed")
    parser.add_argument("--prompt", metavar="ID", help="Print the prompt for one scenario, e.g. S-05")
    parser.add_argument("--all-prompts", action="store_true", help="Write all_prompts.txt")
    parser.add_argument("--add", nargs=2, metavar=("ID", "MODEL"),
                        help="Save reply.txt as a result for this scenario and model")
    parser.add_argument("--score", metavar="FILE", help="Score a results file, e.g. results_full.jsonl")
    parser.add_argument("--scenarios", metavar="FILE", help="Use a scenario file other than my_scenarios.jsonl")
    args = parser.parse_args()
    scenarios = load_scenarios(args.scenarios)

    if args.demo:
        return run_demo(scenarios)
    if args.prompt:
        if args.prompt not in scenarios:
            print(f"No scenario with id {args.prompt}", file=sys.stderr)
            return 1
        print(full_prompt_text(scenarios[args.prompt]))
    elif args.all_prompts:
        write_all_prompts(scenarios)
    elif args.add:
        return add_result(args.add[0], args.add[1], scenarios)
    elif args.score:
        score_file(args.score, scenarios)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
