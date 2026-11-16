#!/usr/bin/env python3
"""
Turn a plain text scenario file into a validated scenarios.jsonl.

    python3 make_scenarios.py my_scenarios.txt

You write scenarios in plain text, one block per scenario, blocks
separated by a line containing only --- :

    ID: S-01
    Category: death_saves
    Difficulty: medium
    Character: Abigail
    Starting state:
    {"name": "Abigail", "hp_current": 0, ...}
    Event:
    Abigail rolls a death saving throw. The roll result is given: 4.
    Expected state:
    {"name": "Abigail", "hp_current": 0, ...}
    Checks: death_saves.failures, dead, hp_current
    Common failure: Increments the failures but does not mark her dead.
    Rules note: A roll of 1 to 9 is one failure, three failures is death.
    ---

The two states are the only JSON you have to write, and you write them
by copying from the character's clean file and changing the numbers
that change. Everything else is ordinary text. The Event text can run
over several lines.

The two states are the parts of the character's sheet that this
scenario sets or changes. run_eval.py merges them into the full clean
sheet, so the model always sees and returns the whole sheet.

The Checks line is optional. If you leave it out, every field in the
expected state is checked, which is the right default: every field in
it has exactly one correct value. Every other field on the sheet is
still compared, as a separate copy-accuracy rate. Write a Checks line only
when you deliberately want to score a subset. Also accept is optional too. It lists other answers that are just as
correct as the expected state, for fields where the rules are genuinely
open. It is a JSON object of field path to a list of alternatives:

    Also accept:
    {"conditions": [[]], "resistances": [["Poison", "Bludgeoning", "Piercing", "Slashing"]]}

The path can be any field on the full sheet, not only ones in the
expected state. Alternatives never change the prompt, so adding one
does not make saved results stale. Common failure and
Rules note are also optional, they are documentation for you and for
readers, the scorer never uses them.

This script checks each scenario as it converts: both states must be
valid JSON, every check must exist in the expected state, IDs must be
unique, and if a matching <character>-clean.json file sits in the same
folder, identity fields in the starting state (hp_max, armor_class,
level, abilities) are compared against it and mismatches are flagged.

Output: <name>.jsonl ready for run_eval.py, and <name>.csv for
reviewing in Numbers or any spreadsheet.
"""

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
FIELDS = ("id", "category", "difficulty", "character", "event",
          "checks", "common_failure", "rules_note")
IDENTITY_FIELDS = ("hp_max", "armor_class", "level", "abilities")


def split_blocks(text):
    blocks, current = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if line.strip() == "---":
            blocks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if any(line.strip() for line in current):
        blocks.append("\n".join(current))
    return [b for b in blocks if b.strip() and not b.lstrip().startswith("#")]


def extract_json(text, label, problems):
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        problems.append(f"{label}: no JSON object found")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        problems.append(f"{label}: JSON does not parse: {exc}")
        return None


def leaf_paths(obj, prefix=""):
    """Every leaf field path in a state, dotted for nested dicts.
    Lists and scalars are leaves, so conditions compares as a whole."""
    paths = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict) and value:
                paths.extend(leaf_paths(value, path + "."))
            else:
                paths.append(path)
    return paths


def get_field(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def parse_block(block, problems):
    scenario = {}
    # Split the block into labelled sections. A label is a known field
    # name at the start of a line, followed by a colon.
    label_pattern = re.compile(
        r"^(ID|Category|Difficulty|Character|Starting state|Event|Expected state|Also accept|Checks|Common failure|Rules note)\s*:\s*(.*)$",
        re.I)
    sections = {}
    current_label = None
    for line in block.splitlines():
        match = label_pattern.match(line.strip())
        if match:
            current_label = match.group(1).lower().replace(" ", "_")
            sections[current_label] = [match.group(2)]
        elif current_label:
            sections[current_label].append(line)
    for key, lines in sections.items():
        sections[key] = "\n".join(lines).strip()

    scenario["id"] = sections.get("id", "")
    if not scenario["id"]:
        problems.append("a scenario is missing its ID line")
    scenario["category"] = sections.get("category", "")
    scenario["difficulty"] = sections.get("difficulty", "")
    scenario["character"] = sections.get("character", "")
    scenario["event"] = " ".join((sections.get("event", "")).split())
    scenario["common_failure"] = sections.get("common_failure", "")
    scenario["rules_note"] = sections.get("rules_note", "")
    scenario["starting_state"] = extract_json(sections.get("starting_state", ""), f"{scenario['id']} starting state", problems)
    scenario["expected_state"] = extract_json(sections.get("expected_state", ""), f"{scenario['id']} expected state", problems)
    scenario["also_accept"] = {}
    if sections.get("also_accept"):
        also = extract_json(sections["also_accept"], f"{scenario['id']} also accept", problems)
        if isinstance(also, dict):
            for path, options in also.items():
                if not isinstance(options, list) or not options:
                    problems.append(f"{scenario['id']}: also accept '{path}' must be a list of "
                                    "alternative values, e.g. {\"conditions\": [[]]}")
            scenario["also_accept"] = also
    raw_checks = sections.get("checks", "")
    scenario["checks"] = [c.strip() for c in re.split(r"[,\n]", raw_checks) if c.strip()]
    if not scenario["checks"] and isinstance(scenario.get("expected_state"), dict):
        # No Checks line: check every field in the expected state.
        scenario["checks"] = leaf_paths(scenario["expected_state"])
    if scenario["expected_state"] is not None:
        for check in scenario["checks"]:
            _, found = get_field(scenario["expected_state"], check)
            if not found:
                problems.append(f"{scenario['id']}: check '{check}' is not a field in the expected state")
    return scenario


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 make_scenarios.py my_scenarios.txt")
        sys.exit(1)
    source = Path(sys.argv[1])
    text = source.read_text()
    problems = []
    scenarios = []
    seen_ids = set()
    for block in split_blocks(text):
        scenario = parse_block(block, problems)
        if scenario["id"] in seen_ids:
            problems.append(f"{scenario['id']}: duplicate ID")
        seen_ids.add(scenario["id"])
        scenarios.append(scenario)

    # Identity check against a base character file, when one is present.
    for scenario in scenarios:
        character = (scenario.get("character") or "").strip().lower()
        base_path = HERE / f"{character}-clean.json"
        if character and base_path.exists() and isinstance(scenario.get("starting_state"), dict):
            base = json.loads(base_path.read_text())
            for field in IDENTITY_FIELDS:
                if field in scenario["starting_state"] and field in base:
                    if scenario["starting_state"][field] != base[field]:
                        problems.append(
                            f"{scenario['id']}: starting state {field} = "
                            f"{scenario['starting_state'][field]!r} does not match {base_path.name} "
                            f"({base[field]!r}). Identity fields must match the base character.")

    # The model is told to return exactly the starting field names, so the
    # answer key has to follow the same rule (action_result is the one
    # allowed addition, for impossible actions).
    for scenario in scenarios:
        start, expected = scenario.get("starting_state"), scenario.get("expected_state")
        if isinstance(start, dict) and isinstance(expected, dict):
            missing = set(start) - set(expected)
            added = set(expected) - set(start) - {"action_result"}
            if missing:
                problems.append(f"{scenario['id']}: expected state drops {sorted(missing)}")
            if added:
                problems.append(f"{scenario['id']}: expected state adds {sorted(added)}, "
                                "which the model is told never to do")

    # Also accept paths must name a real field, in the expected state or on
    # the character's sheet.
    for scenario in scenarios:
        character = (scenario.get("character") or "").strip().lower()
        base_path = HERE / f"{character}-clean.json"
        sheet = json.loads(base_path.read_text()) if base_path.exists() else {}
        for path in scenario.get("also_accept", {}):
            in_expected = get_field(scenario.get("expected_state") or {}, path)[1]
            if not in_expected and not get_field(sheet, path)[1]:
                problems.append(f"{scenario['id']}: also accept '{path}' is not a field in the "
                                f"expected state or in {base_path.name}")

    if problems:
        print(f"{len(problems)} problem(s) found, nothing was written:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)

    jsonl_path = source.with_suffix(".jsonl")
    with open(jsonl_path, "w") as f:
        for scenario in scenarios:
            ordered = {k: scenario[k] for k in
                       ("id", "category", "difficulty", "character", "starting_state",
                        "event", "expected_state", "also_accept", "checks",
                        "common_failure", "rules_note")}
            f.write(json.dumps(ordered) + "\n")
    csv_path = source.with_suffix(".csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "category", "difficulty", "character", "event",
                         "checks", "common_failure", "rules_note"])
        for scenario in scenarios:
            writer.writerow([scenario["id"], scenario["category"], scenario["difficulty"],
                             scenario["character"], scenario["event"],
                             "; ".join(scenario["checks"]), scenario["common_failure"],
                             scenario["rules_note"]])
    print(f"{len(scenarios)} scenario(s) OK. Wrote {jsonl_path.name} and {csv_path.name}.")


if __name__ == "__main__":
    main()
