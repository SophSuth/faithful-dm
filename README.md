# The Faithful DM

**Can a language model keep a D&D character sheet honest?**

The Faithful DM is a small benchmark I built to test whether LLMs track game state correctly under the 2024 Dungeons & Dragons rules (SRD 5.2). Each scenario hands the model a full character sheet and one game event. The model has to return the whole sheet, updated: spend the right spell slot, apply resistance, end concentration when a save fails, refuse an action the character can't take, and change nothing else.

It sounds simple. In practice it catches models skipping rules, quietly inventing new ones, or saying an action happened without actually updating anything.

## Results (November 2026)

| Model | Scenarios passed | Other fields kept | Format slips |
| --- | --- | --- | --- |
| Claude Opus 5.5 | 20 / 20 | 100.0% | 2 |
| Claude Haiku 5.5 | 16 / 20 | 100.0% | 0 |
| Qwen3-4B-2507 | 4 / 20 | 99.7% | 1 |

| Difficulty | Opus 5.5 | Haiku 5.5 | Qwen3-4B |
| --- | --- | --- | --- |
| Easy (5) | 5 | 5 | 4 |
| Medium (11) | 11 | 8 | 0 |
| Hard (4) | 4 | 3 | 0 |

What stood out:

- **Difficulty is nested.** Every scenario Haiku failed, Qwen failed too. No scenario tripped a stronger model while a weaker one passed.
- **Haiku's misses were 2024 rule changes and conditions.** It gave back only half its Hit Dice on a Long Rest, which is the 2014 rule. It never added the Invisible condition, either from the Invisibility spell or from hiding. And it ignored Rage resistance in a scenario whose twin it got right.
- **Qwen describes the action without applying it.** In 9 of its 16 failures it left a slot, a use, concentration or a rest untouched. It also invented rules: a "Psychic Damage" condition, damage going into temp HP, and healing past max HP. And it cast a spell the character didn't have prepared.
- **Copying the sheet isn't the hard part.** Even the 4B model kept 99.7% of the untouched fields exactly. Models fail on rules, not on transcription.

Raw replies for every run are in `results_full.jsonl`.

## How it works

**Characters.** Four level 3 characters built in D&D Beyond and converted into clean JSON sheets of 300 to 570 fields each:

| Character | Class | Species |
| --- | --- | --- |
| Abigail | Wizard 3 (Evoker) | Gold Dragonborn |
| Delilah | Bard 3 (College of Lore) | Human |
| Jimmy | Barbarian 3 (Berserker) | Dwarf |
| Mark | Rogue 3 (Thief) | Drow Elf |

**Scenarios.** 20 single events covering damage, healing, rests, spell slots, concentration, conditions, death saves and limited-use features. Every die roll is given in the event, so there's exactly one correct sheet afterwards. A few are traps on purpose:

- **S-04** is an exact twin of S-02, except Jimmy is raging.
- **S-16** asks Delilah to cast a spell she doesn't have prepared.
- **S-12** only passes under the 2024 Long Rest rules.

Each scenario lists the rules reasoning and the most likely wrong answer in `my_scenarios.txt`.

**Scoring.** Each reply gets three separate numbers:

- **State (pass or fail).** Every field the event should change is right, and nothing is invented. Implied conditions count, since in 2024 Unconscious includes Prone. Where the rules genuinely allow two answers, both are accepted.
- **Other fields kept.** The share of untouched fields copied back exactly. Slips are reported, but they don't fail a scenario.
- **Format.** Whether the reply was bare JSON. Prose or a code fence around it is a slip, but it can still pass.

Every saved reply stores a fingerprint of the exact prompt it answered. If a scenario or sheet changes later, old replies show up as stale instead of being graded against the new answer key.

## Run it yourself

You only need Python 3, with no packages to install.

```
python3 run_eval.py --demo                      # check the scorer works
python3 run_eval.py --score results_full.jsonl  # reproduce the results above
```

### Test a new model

1. Get the prompts: `python3 run_eval.py --prompt S-01` prints one, and `all_prompts.txt` has all 20.
2. Use a fresh chat for every scenario, so earlier answers don't leak into later ones.
3. Paste the model's reply, exactly as written, into a file called `reply.txt`.
4. Save it: `python3 run_eval.py --add S-01 "Model name (where, settings)"`.
5. Score everything: `python3 run_eval.py --score results_full.jsonl`.

### Add or change a scenario

Edit `my_scenarios.txt`. The format is explained at the top of the file. Then run:

```
python3 make_scenarios.py my_scenarios.txt
python3 run_eval.py --all-prompts
```

## Run settings

| Model | Where | Settings | Date |
| --- | --- | --- | --- |
| Claude Opus 5.5 | Claude app (chat) | Medium effort | Nov 11, 2026 |
| Claude Haiku 5.5 | Claude app (chat) | Medium effort | Nov 11, 2026 |
| Qwen3-4B-2507 | LM Studio on a Mac | MLX 4-bit, temperature 0 | Nov 12, 2026 |

Every model got the same system prompt (in `run_eval.py` and `prompt_template.md`) and one scenario per fresh chat.

## Files

| File | What it is |
| --- | --- |
| `run_eval.py` | Prompts, saving replies, scoring |
| `my_scenarios.txt` | The 20 scenarios, with rules notes and expected failures |
| `make_scenarios.py` | Checks `my_scenarios.txt` and builds `my_scenarios.jsonl` and `.csv` |
| `*-clean.json` | The four character sheets |
| `all_prompts.txt` | Every prompt exactly as the models saw it |
| `results_full.jsonl` | Every model reply, verbatim |
| `dndbeyond_to_eval.py` | Converts a D&D Beyond character export into a clean sheet |
| `prompt_template.md` | The prompt and the run protocol |
| `CHANGES.md` | Every fix made to the benchmark, and why |

The raw D&D Beyond exports aren't included (see Licenses below), so the converter is there for anyone who wants to build their own characters.

## Limitations

- 20 scenarios and one run per model, so a single scenario moves a score by 5 points.
- The accepted alternative answers in S-01, S-03, S-04 and S-18 were added after seeing model replies. All are correct under the rules and logged in `CHANGES.md`, but future alternatives should be set before running.
- The sheets include feature descriptions, which sometimes state the rule being tested. That's realistic for a DM, but it's somewhat open book.
- Level 3 only, single events only. Temp HP, damage at 0 HP and casting with no slot left aren't covered yet.

## Licenses

- **Code** (`*.py`): MIT, see `LICENSE`.
- **Scenarios, character sheets, prompts and results**: CC-BY-4.0, see `DATA_LICENSE.md`.

This work includes material from the System Reference Document 5.2 ("SRD 5.2") by Wizards of the Coast LLC, available at https://www.dndbeyond.com/srd. The SRD 5.2 is licensed under the Creative Commons Attribution 4.0 International License, available at https://creativecommons.org/licenses/by/4.0/legalcode.

This is an unofficial project, not affiliated with or endorsed by Wizards of the Coast or D&D Beyond.
