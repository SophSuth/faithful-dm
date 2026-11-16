# Faithful DM: Prompt Template

Use this exact prompt for every model and every scenario. Changing wording between models makes the comparison unfair.

Every prompt hands the model the character's **full sheet** (from `<name>-clean.json`) with the scenario's starting state merged in. Don't build prompts by hand. Generate them so they always match the current scenarios and sheets:

- One scenario: `python3 run_eval.py --prompt S-01`
- All of them: `python3 run_eval.py --all-prompts` writes all_prompts.txt

Regenerate every time you edit my_scenarios.txt, right after `python3 make_scenarios.py my_scenarios.txt`.

## System prompt

You are the state tracker for a Dungeons & Dragons 5e game using the 2024 revised rules (SRD 5.2). You will be given a starting character state as JSON and one game event. All dice results you need are given in the event. Do not roll dice. Do not invent rules, items, features, or resources that are not in the starting state. Return only the updated state as a single JSON object with exactly the same field names as the starting state. If the event includes an action that cannot be done with the resources in the starting state, leave the state unchanged and add a field called action_result with the value failed_no_slot for a spell with no slot, or failed_impossible for any other impossible action. Add no narration, no explanation, and no markdown fences.

## User prompt

The full sheet goes in as JSON on one line, with no code fence around it:

```
Starting state:
{full_sheet_json}

Event:
{event}

Return only the updated state JSON.
```

## How replies are scored

- **State** decides pass or fail. Every field the scenario changes or sets must be right, and the model can't invent fields. Conditions count implied ones (Unconscious includes Incapacitated and Prone).
- **Kept fields** is reported separately. This is the share of all other sheet fields that came back unchanged. Copy slips are listed but don't fail the scenario.
- **Format** is also separate. A reply in a code fence, or with a sentence around it, is a format slip, but it can still pass.

## Saving a reply

1. Paste the model's reply, exactly as written, into reply.txt and save.
2. Run `python3 run_eval.py --add S-01 "Model Name"`.
3. Score any time with `python3 run_eval.py --score results_full.jsonl`.

Each saved reply gets a fingerprint of the prompt it answered. If you edit that scenario or regenerate a sheet later, the scorer marks the reply as stale instead of grading it against the new answer key. reply.txt is emptied after saving so an old reply can't be saved twice.

## Run settings to log for every run

- Model name and exact version
- Date run
- Temperature, use 0 if available
- Provider and interface, API or chat window
- Any system prompt changes, there should be none after you start

## If you test in a chat window instead of an API

Start a fresh chat for each scenario. Models remember earlier states in the same chat, and that memory will contaminate the next scenario. One scenario, one fresh chat, paste, save the raw reply, close it.
