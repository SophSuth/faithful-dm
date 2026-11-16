# Changelog

How the benchmark got to its current version. Every change that affected scoring is here, along with what prompted it. Several were caught by the models themselves while taking the test.

## Nov 12, 2026

- **Copy slips in lists are matched item by item, not by position.** Qwen's S-08 reply added one extra Dagger, which shifted every later inventory item and showed up as 48 copy slips. It now counts once. Items that only changed order aren't slips at all, since order isn't game state.
- Qwen3-4B results added.

## Nov 11, 2026: first full runs (Opus 5.5, Haiku 5.5)

**Answer key fixes, found by model replies:**

- **S-14** now expects the Invisible condition. The 2024 Invisibility spell gives it, and Opus pointed out the key was missing it.
- **Delilah's sheet** listed her spells as prepared in one place and unprepared in another, because D&D Beyond leaves the flag off for Bards. The converter now keeps both in sync. Opus flagged this too.
- **S-18** accepts `tremorsense: 60` added to `senses`, since Stonecunning grants exactly that. Both Opus and Haiku wrote it.
- **S-01** accepts dropping Unconscious once the character is dead, because the 2024 rules don't settle it. **S-03 and S-04** accept Rage's resistances written into `resistances`.

**Scorer:**

- New optional `Also accept:` line in `my_scenarios.txt` for fields where the rules allow more than one answer. Alternatives don't change the prompt, so they never invalidate saved results.
- `resistances`, `immunities` and `vulnerabilities` compare as sets.
- Cut-off replies are labelled "truncated" so capture problems get caught and rerun. Three replies were rerun for this reason: two cut off mid-paste, and one saved under the wrong scenario ID.

## Nov 11, 2026: full sheets only

The first pilot also tested a "trimmed" format, where the model saw only the fields a scenario touched. I dropped it to keep one clear setup: the model always gets the complete character sheet and returns all of it. The scenarios still list only the fields they set or change, and `run_eval.py` merges them into the full sheet.

## Nov 11, 2026: review before the first full runs

**Scenarios:**

- **S-04** made an exact twin of S-02 (same HP and damage, only Rage differs), so the pair isolates resistance.
- **S-09** gives the bare d20 roll and Mark's ability scores. The first version gave an ambiguous "roll of 8" and expected a failed save, which was only right if 8 already included his +2 Con.
- **S-16** gives a healing roll like every other spell event. Without it, the missing roll hinted that the cast was meant to fail.
- **S-19** specifies Total Cover, which Hide requires.
- **S-20** uses the 2024 rule that anyone can drink a potion as a Bonus Action. The first version credited the Thief's Fast Hands, which is a 2014 reading.
- **S-08** no longer tells the model Faerie Fire needs concentration, consistent with the other spell scenarios.

**Scorer:**

- Pass or fail is about state only. Formatting (code fences, prose around the JSON) is tracked as a separate count.
- Fields the model invents fail the scenario, including an `action_result` on an action that worked.
- Conditions compare as sets, with implied conditions expanded (Unconscious includes Incapacitated and Prone).
- `true` is no longer accepted as `1`.
- Every saved reply carries a fingerprint of its exact prompt, so replies to older versions of a scenario show as stale instead of being scored against a newer key.
- Copying untouched fields is reported as its own rate, so one transcription slip doesn't fail a scenario.

**Converter:**

- D&D Beyond's `{{template}}` placeholders in feature text are filled in from the character's own numbers, instead of being left blank.
- HTML tags are stripped, spell names are kept plain, and weapon masteries are labelled correctly.
