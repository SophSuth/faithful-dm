#!/usr/bin/env python3
"""
Convert a raw D&D Beyond character JSON export into a clean Faithful DM
starting state.

    python3 dndbeyond_to_eval.py raw_character.json -o clean_character.json

Important, read this before trusting the output:
D&D Beyond's raw JSON is an ingredient list, not a finished character sheet.
It stores base ability scores, base hit points, and every item with an
equipped flag. It does NOT store most final totals. Armour class, max HP,
saving throws, skill bonuses, and spell slot maximums are calculated by
D&D Beyond's own engine when it draws the sheet.

This converter extracts everything that is stored directly, computes AC
and max HP from the stored ingredients, including the character's
modifier list (ability score increases, hit points per level bonuses,
unarmoured defence and the class spell slot table). The output is kept
clean, with no notes sections: check the calculated fields against the
numbers on the D&D Beyond sheet itself, once per character, and correct
the clean file if they differ.
"""

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path

ABILITY_IDS = {1: "strength", 2: "dexterity", 3: "constitution",
               4: "intelligence", 5: "wisdom", 6: "charisma"}

CONDITION_IDS = {1: "blinded", 2: "charmed", 3: "deafened", 4: "frightened",
                 5: "grappled", 6: "incapacitated", 7: "invisible",
                 8: "paralyzed", 9: "petrified", 10: "poisoned", 11: "prone",
                 12: "restrained", 13: "stunned", 14: "unconscious",
                 15: "exhaustion"}

ARMOR_TYPE_IDS = {1: "light", 2: "medium", 3: "heavy", 4: "shield"}


def modifier(score):
    return math.floor((score - 10) / 2)


def load_character(path):
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
        return raw["data"]
    return raw


def iter_modifiers(data):
    """Yield every granted modifier in the character's modifier list.

    The raw export ends with a "modifiers" object, grouped by source
    (race, class, background, feat, item). That list is where D&D Beyond
    stores the rules it applies when it calculates the sheet: ability
    score increases from a background, hit points per level bonuses like
    Dwarven Toughness, unarmoured defence, and armour class bonuses.
    The totals themselves are never stored, but these instructions are,
    so a converter that ignores this list gets characters wrong.
    """
    groups = data.get("modifiers") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                for entry in group:
                    if isinstance(entry, dict) and entry.get("isGranted", True):
                        yield entry


def iter_all_modifiers(data):
    """Yield every modifier in the list, including the ones flagged
    isGranted false. That flag does not mean inactive: D&D Beyond marks
    every modifier that comes from a player choice (a picked skill
    proficiency, a chosen language, a Scholar expertise) as not granted,
    even though the sheet applies it. Automatic grants are flagged true.
    The ability score, hit point, and armour calculations use the
    stricter iter_modifiers because those were verified against the
    sheet that way; proficiency style data must use this one or every
    chosen proficiency silently disappears."""
    groups = data.get("modifiers") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                for entry in group:
                    if isinstance(entry, dict):
                        yield entry


def modifier_amount(entry):
    for key in ("fixedValue", "value"):
        if entry.get(key) is not None:
            return entry[key]
    return 0


def ability_scores(data):
    base = {ABILITY_IDS[s["id"]]: s.get("value") for s in data.get("stats", []) if s.get("id") in ABILITY_IDS}
    bonus = {ABILITY_IDS[s["id"]]: (s.get("value") or 0) for s in data.get("bonusStats", []) if s.get("id") in ABILITY_IDS}
    override = {ABILITY_IDS[s["id"]]: s.get("value") for s in data.get("overrideStats", []) if s.get("id") in ABILITY_IDS}
    scores = {}
    for ability in ABILITY_IDS.values():
        if override.get(ability) is not None:
            scores[ability] = override[ability]
        else:
            scores[ability] = (base.get(ability) or 10) + bonus.get(ability, 0)
    # Ability score increases live in the modifier list, not in stats.
    # A 2024 background grants them through a feat, for example +1 to
    # three scores, and the stats array does not include them. They are
    # read from the unfiltered list: some characters' increases are
    # flagged isGranted false because they come from a choice (a
    # Human's ability increases in Delilah's export), and the stricter
    # reading silently loses all three points.
    for entry in iter_all_modifiers(data):
        subtype = entry.get("subType") or ""
        if not subtype.endswith("-score"):
            continue
        ability = subtype[: -len("-score")]
        if ability not in scores:
            continue
        if entry.get("type") == "bonus":
            scores[ability] += modifier_amount(entry)
        elif entry.get("type") in ("set", "set-base"):
            scores[ability] = modifier_amount(entry)
    return scores


def class_summary(data):
    parts = []
    total_level = 0
    for cls in data.get("classes", []):
        level = cls.get("level") or 0
        total_level += level
        definition = cls.get("definition") or {}
        name = definition.get("name") or "Unknown class"
        subclass = (cls.get("subclassDefinition") or {}).get("name")
        parts.append(f"{name} {level}" + (f" ({subclass})" if subclass else ""))
    return ", ".join(parts), total_level


def inventory_items(data):
    items = []
    for entry in data.get("inventory", []):
        definition = entry.get("definition") or {}
        item = {
            "name": definition.get("name") or "Unnamed item",
            "quantity": entry.get("quantity", 1),
            "equipped": bool(entry.get("equipped")),
        }
        if definition.get("armorClass") is not None:
            item["base_armor_class"] = definition.get("armorClass")
            item["armor_type"] = ARMOR_TYPE_IDS.get(definition.get("armorTypeId"), "unknown")
        if entry.get("isAttuned"):
            item["attuned"] = True
        items.append(item)
    return items


WEAPON_CATEGORIES = {1: "simple-weapons", 2: "martial-weapons"}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def weapon_attacks(data, ability_mods=None, prof_bonus=0, proficiency_subtypes=()):
    """Build an attacks list from weapon items in the inventory.

    The raw export stores the damage dice, damage type, range, and
    properties of a weapon in its item definition. It does NOT store the
    final attack bonus or damage bonus, so those are calculated here by
    the standard formula: the relevant ability modifier, plus the
    proficiency bonus when the character is proficient with the weapon,
    plus any flat attack or damage bonuses the item itself grants (a
    magic weapon's +1, for example). Fighting styles and feature based
    adjustments (Archery, Dueling, a Monk's Martial Arts die) are NOT
    in that formula, they are listed as known gaps in RAW_JSON_AUDIT.md.
    """
    attacks = []
    profs = set(proficiency_subtypes)
    for entry in data.get("inventory", []):
        definition = entry.get("definition") or {}
        damage = definition.get("damage") or {}
        if definition.get("filterType") != "Weapon" and not damage.get("diceString"):
            continue
        properties = [p.get("name") for p in definition.get("properties") or [] if p.get("name")]
        attack_kind = {1: "melee", 2: "ranged"}.get(definition.get("attackType"), "unknown")
        attack_bonus = damage_bonus = None
        proficient = None
        if ability_mods is not None:
            if attack_kind == "ranged":
                ability_mod = ability_mods.get("dexterity", 0)
            elif "Finesse" in properties:
                ability_mod = max(ability_mods.get("strength", 0),
                                   ability_mods.get("dexterity", 0))
            else:
                ability_mod = ability_mods.get("strength", 0)
            category = WEAPON_CATEGORIES.get(definition.get("categoryId"))
            proficient = bool(
                (category and category in profs)
                or slugify(definition.get("type")) in profs
                or slugify(definition.get("name")) in profs
            )
            magic_attack = magic_damage = 0
            for granted in definition.get("grantedModifiers") or []:
                amount = granted.get("fixedValue")
                if amount is None:
                    amount = granted.get("value")
                if not isinstance(amount, (int, float)):
                    continue
                if granted.get("subType") == "weapon-attack-rolls":
                    magic_attack += amount
                elif granted.get("subType") == "weapon-damage-rolls":
                    magic_damage += amount
            attack_bonus = ability_mod + (prof_bonus if proficient else 0) + magic_attack
            damage_bonus = ability_mod + magic_damage
        attacks.append({
            "name": definition.get("name"),
            "equipped": bool(entry.get("equipped")),
            "attack_type": attack_kind,
            "damage_dice": damage.get("diceString"),
            "damage_type": definition.get("damageType"),
            "range": definition.get("range"),
            "long_range": definition.get("longRange"),
            "properties": properties,
            "proficient": proficient,
            "attack_bonus": attack_bonus,
            "damage_bonus": damage_bonus,
        })
    return attacks


COMPONENT_IDS = {1: "V", 2: "S", 3: "M"}


def casting_time_text(definition, entry):
    act = entry.get("activation") or definition.get("activation") or {}
    amount, kind = act.get("activationTime"), act.get("activationType")
    if kind == 1:
        return "1 action"
    if kind == 3:
        return "1 bonus action"
    if kind == 4:
        return "1 reaction"
    if kind == 5:
        return f"{amount or 1} minute" + ("s" if (amount or 1) != 1 else "")
    if kind == 6:
        return f"{amount or 1} hour" + ("s" if (amount or 1) != 1 else "")
    if kind == 7:
        return "special"
    return None


def range_text(rng):
    if not isinstance(rng, dict):
        return None
    origin, value = rng.get("origin"), rng.get("rangeValue")
    aoe = ""
    if rng.get("aoeValue") and rng.get("aoeType"):
        aoe = f" ({rng['aoeValue']}-foot {str(rng['aoeType']).lower()})"
    if value:
        return f"{value} ft."
    if origin:
        return origin + aoe
    return None


def duration_text(dur):
    if not isinstance(dur, dict):
        return None
    kind = dur.get("durationType")
    interval, unit = dur.get("durationInterval"), (dur.get("durationUnit") or "")
    if kind == "Instantaneous":
        return "Instantaneous"
    if kind == "Concentration":
        if interval:
            return f"Concentration, up to {interval} {unit.lower()}" + ("s" if interval != 1 else "")
        return "Concentration"
    if kind == "Time" and interval:
        return f"{interval} {unit.lower()}" + ("s" if interval != 1 else "")
    return kind


def description_damage(definition):
    """Pull the first damage dice out of a spell's description text.

    Spell definitions in the export rarely carry damage dice as data.
    The damage lives in the description, in sentences like "the target
    takes 1d10 Fire damage", so read it from there and label the source.
    """
    text = re.sub(r"<[^>]+>", " ", definition.get("description") or "")
    text = html.unescape(text)
    match = re.search(r"(\d+d\d+(?:\s*\+\s*\d+)?)\s+(?:\w+\s+)?damage", text, re.I)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    return None


RESET_TYPES = {1: "short rest", 2: "long rest"}


WEAPON_MASTERY_PROPERTIES = {"Cleave", "Graze", "Nick", "Push", "Sap", "Slow", "Topple", "Vex"}


def template_value(token, ctx):
    """Evaluate one D&D Beyond {{template}} like {{(classlevel/2)@roundup}},
    {{modifier:cha#unsigned}} or {{savedc:con}}. Returns the text to put in
    its place, or None when the value cannot be worked out from the export.
    """
    body = token.strip("{}").strip().lower()
    expr = re.split(r"[@#]", body, maxsplit=1)[0]
    flags = set(re.split(r"[@#,]", body)[1:])
    if expr == "scalevalue":
        return ctx.get("scalevalue")
    mods = ctx.get("ability_mods") or {}
    scores = ctx.get("ability_scores") or {}
    abbrev = {"str": "strength", "dex": "dexterity", "con": "constitution",
              "int": "intelligence", "wis": "wisdom", "cha": "charisma"}
    prof = ctx.get("prof_bonus")

    def stat(match):
        kind, key = match.group(1), abbrev.get(match.group(2))
        if key is None or prof is None and kind in ("savedc", "spellattack"):
            raise KeyError(match.group(0))
        if kind == "modifier":
            return str(mods[key])
        if kind == "abilityscore":
            return str(scores[key])
        if kind == "savedc":
            return str(8 + prof + mods[key])
        return str(prof + mods[key])

    try:
        expr = re.sub(r"(modifier|abilityscore|savedc|spellattack):(\w+)", stat, expr)
        names = {"classlevel": ctx.get("level"), "characterlevel": ctx.get("level"),
                 "proficiency": prof, "limiteduse": ctx.get("uses_max")}
        for name, value in names.items():
            if name in expr:
                if value is None:
                    return None
                expr = expr.replace(name, str(value))
        if not re.fullmatch(r"[\d\s+\-*/().]+", expr):
            return None
        value = eval(expr, {"__builtins__": {}})  # digits and operators only, checked above
    except (KeyError, SyntaxError, ZeroDivisionError, TypeError):
        return None
    value = math.ceil(value) if "roundup" in flags else math.floor(value)
    for flag in flags:
        if flag.startswith("min:") and flag[4:].isdigit():
            value = max(value, int(flag[4:]))
    return f"{value:+d}" if "signed" in flags else str(value)


def clean_snippet(text, ctx=None):
    """Make an action snippet readable: strip HTML and markdown bold,
    collapse whitespace, and fill in D&D Beyond's {{template}} values from
    the character's own numbers. A template that cannot be worked out is
    kept as written rather than deleted, so the sentence never ends up
    with a silent hole in it."""
    if not text:
        return None
    ctx = ctx or {}

    def fill(match):
        value = template_value(match.group(0), ctx)
        return match.group(0) if value is None else value

    text = re.sub(r"\{\{[^}]*\}\}", fill, text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = text.replace("*", "")
    return " ".join(text.split())[:300] or None


def legitimate_components(data):
    """The set of component ids that belong to this character's own
    build: class and subclass features, species traits, the options
    those features offer, feats, background, and items. Campaign
    granted actions (a shared campaign feature that hands every
    character the same extra actions) carry component ids that exist
    nowhere in the character's own data, which is how they are told
    apart from real actions."""
    ids = set()

    def add(value):
        if isinstance(value, int):
            ids.add(value)

    for cls in data.get("classes") or []:
        for holder in (cls, cls.get("definition") or {},
                       cls.get("subclassDefinition") or {}):
            add(holder.get("id"))
            for feature in holder.get("classFeatures") or []:
                if isinstance(feature, dict):
                    add(feature.get("id"))
                    add((feature.get("definition") or {}).get("id"))
    race = data.get("race") or {}
    add(race.get("id"))
    for trait in race.get("racialTraits") or []:
        add(trait.get("id"))
        add((trait.get("definition") or {}).get("id"))
    add(((data.get("background") or {}).get("definition") or {}).get("id"))
    for feat in data.get("feats") or []:
        add((feat.get("definition") or {}).get("id"))
    options = data.get("options") or {}
    if isinstance(options, dict):
        for lst in options.values():
            if isinstance(lst, list):
                for entry in lst:
                    if isinstance(entry, dict):
                        add((entry.get("definition") or {}).get("id"))
    for entry in data.get("inventory") or []:
        add((entry.get("definition") or {}).get("id"))
    return ids


def extract_features(data, prof_bonus, ability_mods=None, ability_scores=None):
    """Pull the character's features out of the raw export.

    Three kinds matter for the eval. Always on traits that change how
    events resolve, like damage resistances, which are not baked into
    any number on the sheet. Stateful features with uses and an on or
    off state, which live in the actions section with their limited
    use counts (Rage is the classic case: 3 uses, long rest reset).
    And plain feature names from class, species, and feats, so a
    scenario can reference what the character has.
    """
    features = []
    feature_uses = {}
    level = sum((cls.get("level") or 0) for cls in data.get("classes", [])) or None
    actions = data.get("actions") or {}
    legit = legitimate_components(data)
    seen = set()
    if isinstance(actions, dict):
        for group, lst in actions.items():
            if not isinstance(lst, list):
                continue
            for action in lst:
                if not isinstance(action, dict):
                    continue
                name = action.get("name")
                if not name or name in seen:
                    continue
                if action.get("componentId") not in legit:
                    continue
                seen.add(name)
                # D&D Beyond files weapon mastery actions under "feat".
                if name.split(" (")[0] in WEAPON_MASTERY_PROPERTIES:
                    group = "weapon mastery"
                entry = {"name": name, "source": group}
                uses_max = None
                limited = action.get("limitedUse")
                if isinstance(limited, dict):
                    uses_max = limited.get("maxUses") or None
                    if uses_max is None and limited.get("useProficiencyBonus"):
                        uses_max = prof_bonus
                    if uses_max is None and limited.get("statModifierUsesId") \
                            and ability_mods is not None:
                        # Bardic Inspiration is the standard case: the
                        # uses are an ability modifier (Charisma), and
                        # the export stores which ability, not a number.
                        stat = ABILITY_IDS.get(limited["statModifierUsesId"])
                        if stat:
                            uses_max = ability_mods.get(stat)
                    if uses_max is not None:
                        used = limited.get("numberUsed") or 0
                        entry["uses_max"] = uses_max
                        entry["uses_remaining"] = uses_max - used
                        if limited.get("resetType") in RESET_TYPES:
                            entry["resets_on"] = RESET_TYPES[limited["resetType"]]
                        feature_uses[name] = {
                            "uses_max": uses_max,
                            "uses_remaining": uses_max - used,
                            "resets_on": entry.get("resets_on"),
                        }
                dice = action.get("dice") or {}
                if dice.get("diceString"):
                    entry["dice"] = dice["diceString"]
                if action.get("value") is not None:
                    entry["value"] = action["value"]
                ctx = {"uses_max": uses_max, "prof_bonus": prof_bonus,
                       "ability_mods": ability_mods, "ability_scores": ability_scores,
                       "level": level,
                       "scalevalue": (dice.get("diceString") or "").lstrip("1") or None}
                summary = clean_snippet(action.get("snippet"), ctx)
                if summary:
                    entry["summary"] = summary
                features.append(entry)

    resistances, vulnerabilities, immunities = [], [], []
    buckets = {"resistance": resistances, "vulnerability": vulnerabilities,
               "immunity": immunities, "condition-immunity": immunities}
    for entry in iter_modifiers(data):
        bucket = buckets.get(entry.get("type"))
        if bucket is not None and entry.get("subType") and entry["subType"] not in bucket:
            bucket.append(entry["subType"])

    class_features = []
    for cls in data.get("classes", []):
        class_level = cls.get("level") or 0
        for holder in (cls, cls.get("subclassDefinition") or {}):
            for feature in holder.get("classFeatures") or []:
                if isinstance(feature, dict):
                    definition = feature.get("definition") or feature
                    name = definition.get("name")
                    # The export lists every feature the class will ever
                    # get. Keep only the ones this character has reached.
                    required = definition.get("requiredLevel")
                    if required is not None and required > class_level:
                        continue
                    if name and name not in class_features:
                        class_features.append(name)
    species_traits = []
    speed_ft = None
    race = data.get("race") or {}
    for trait in race.get("racialTraits") or []:
        definition = (trait or {}).get("definition") or {}
        name = definition.get("name")
        if name and name not in species_traits:
            species_traits.append(name)
        if name == "Speed":
            match = re.search(r"(\d+)\s*ft", definition.get("snippet") or "")
            if match:
                speed_ft = int(match.group(1))
    if speed_ft is None:
        for entry in iter_modifiers(data):
            if entry.get("subType") in ("walking-speed", "speed") \
                    and entry.get("type") in ("set", "set-base"):
                amount = modifier_amount(entry)
                if isinstance(amount, (int, float)):
                    speed_ft = amount
    if speed_ft is not None:
        for entry in iter_modifiers(data):
            if entry.get("subType") == "speed" and entry.get("type") == "bonus":
                amount = modifier_amount(entry)
                if isinstance(amount, (int, float)):
                    speed_ft += amount
    # The feats array is polluted. Alongside real feats it carries
    # campaign features (renown tracks, boons) that D&D Beyond itself
    # tags "__DISGUISE_FEAT", they are not feats and the player never
    # chose them, plus the background's ability score improvement
    # container, which is machinery whose effect is already inside the
    # ability scores. A feat is kept only when something real granted
    # it (background, species, or class granted feat lists) or it
    # carries a genuine feat category like Origin or General.
    granted_feat_ids = set()
    grant_sources = [(data.get("background") or {}).get("definition") or {},
                     race]
    for cls in data.get("classes", []):
        grant_sources.append(cls.get("definition") or {})
        grant_sources.append(cls.get("subclassDefinition") or {})
    for source in grant_sources:
        for granted in source.get("grantedFeats") or []:
            if isinstance(granted, dict) and granted.get("id") is not None:
                granted_feat_ids.add(granted["id"])
        for feat_id in source.get("featIds") or []:
            granted_feat_ids.add(feat_id)
    feats = []
    for feat in data.get("feats") or []:
        if not isinstance(feat, dict):
            continue
        definition = feat.get("definition") or feat
        name = definition.get("name")
        if not name:
            continue
        tags = {c.get("tagName") for c in definition.get("categories") or []
                if isinstance(c, dict)}
        if "__DISGUISE_FEAT" in tags or "__INITIAL_ASI" in tags:
            continue
        real_tags = tags - {"__DISPLAY_WITH_DATA_ORIGIN"}
        if feat.get("componentId") in granted_feat_ids or real_tags:
            if name not in feats:
                feats.append(name)
    return {
        "speed_ft": speed_ft,
        "resistances": resistances,
        "vulnerabilities": vulnerabilities,
        "immunities": immunities,
        "features": features,
        "feature_uses": feature_uses,
        "class_features": class_features,
        "species_traits": species_traits,
        "feats": feats,
    }


SKILL_ABILITIES = {
    "acrobatics": "dexterity", "animal-handling": "wisdom",
    "arcana": "intelligence", "athletics": "strength",
    "deception": "charisma", "history": "intelligence",
    "insight": "wisdom", "intimidation": "charisma",
    "investigation": "intelligence", "medicine": "wisdom",
    "nature": "intelligence", "perception": "wisdom",
    "performance": "charisma", "persuasion": "charisma",
    "religion": "intelligence", "sleight-of-hand": "dexterity",
    "stealth": "dexterity", "survival": "wisdom",
}

SENSE_SUBTYPES = {"darkvision", "blindsight", "tremorsense", "truesight"}


APOSTROPHE_FIXES = {"calligraphers": "Calligrapher's", "thieves": "Thieves'",
                     "brewers": "Brewer's", "cartographers": "Cartographer's",
                     "cobblers": "Cobbler's", "glassblowers": "Glassblower's",
                     "jewelers": "Jeweler's", "leatherworkers": "Leatherworker's",
                     "masons": "Mason's", "painters": "Painter's",
                     "potters": "Potter's", "smiths": "Smith's",
                     "tinkers": "Tinker's", "weavers": "Weaver's",
                     "woodcarvers": "Woodcarver's"}


def readable(slug):
    words = []
    for word in (slug or "").replace("-", " ").split():
        words.append(APOSTROPHE_FIXES.get(word.lower(), word.capitalize()))
    return " ".join(words)


def extract_choices(data):
    """Resolve the choices the player actually made (skills picked,
    expertise picked, spells chosen through a feat, languages) into
    readable pairs. The raw file stores picks as numeric option ids;
    the labels live in the choice definitions, the spell and item
    definitions, and the definition name map."""
    choices = data.get("choices") or {}
    labels = {}
    for cdef in choices.get("choiceDefinitions") or []:
        for opt in cdef.get("options") or []:
            if opt.get("id") is not None and opt.get("label"):
                labels[opt["id"]] = opt["label"]
    for key, name in (choices.get("definitionKeyNameMap") or {}).items():
        try:
            labels[int(str(key).rsplit(":", 1)[-1])] = name
        except (TypeError, ValueError):
            pass

    def note(definition):
        if isinstance(definition, dict) and definition.get("id") is not None \
                and definition.get("name"):
            labels.setdefault(definition["id"], definition["name"])

    for entry in data.get("classSpells") or []:
        note(entry.get("definition"))
    spells = data.get("spells") or {}
    if isinstance(spells, dict):
        for lst in spells.values():
            if isinstance(lst, list):
                for entry in lst:
                    note((entry or {}).get("definition"))
    for entry in data.get("inventory") or []:
        note(entry.get("definition"))

    made, seen = [], set()
    for group, lst in choices.items():
        if group in ("choiceDefinitions", "definitionKeyNameMap") \
                or not isinstance(lst, list):
            continue
        for entry in lst:
            if not isinstance(entry, dict) or entry.get("optionValue") is None:
                continue
            picked = labels.get(entry["optionValue"])
            if not picked:
                continue
            pair = (entry.get("label") or group, picked)
            if pair not in seen:
                seen.add(pair)
                made.append({"choice": pair[0], "picked": picked, "source": group,
                             "componentId": entry.get("componentId")})
    return made


def extract_derived(data, ability_mods, prof_bonus):
    """Everything the sheet calculates from the modifier list that the
    raw file never writes down as a finished number: saving throw and
    skill totals (modifier plus proficiency, plus expertise where it
    applies), initiative, passive Perception, languages, senses, the
    remaining proficiencies, hit dice, and the player's choices.

    Only flat, always on bonuses are summed. Anything conditional (a
    bonus that applies only sometimes) is a known gap, see
    RAW_JSON_AUDIT.md.
    """
    entries = list(iter_all_modifiers(data))

    def subtypes(kind):
        return {e.get("subType") for e in entries
                if e.get("type") == kind and e.get("subType")}

    def bonus_total(wanted):
        total = 0
        for e in entries:
            if e.get("type") == "bonus" and e.get("subType") in wanted:
                amount = modifier_amount(e)
                if isinstance(amount, (int, float)):
                    total += amount
        return total

    proficient = subtypes("proficiency")
    expertise = subtypes("expertise")
    half = subtypes("half-proficiency")

    saving_throws = {}
    for ability in ABILITY_IDS.values():
        sub = ability + "-saving-throws"
        saving_throws[ability] = {
            "total": ability_mods[ability]
                     + (prof_bonus if sub in proficient else 0)
                     + bonus_total({sub, "saving-throws"}),
            "proficient": sub in proficient,
        }

    skills = {}
    # Jack of All Trades style half proficiency is stored as a single
    # entry with the subType "ability-checks", not per skill. When it
    # is present, every skill the character is not already proficient
    # in gets half the proficiency bonus, which is also how the sheet
    # prints those skills.
    half_all = "ability-checks" in half
    for slug, ability in SKILL_ABILITIES.items():
        total = ability_mods[ability] + bonus_total({slug})
        if slug in proficient:
            total += prof_bonus
        if slug in expertise:
            total += prof_bonus
        elif slug in half or half_all:
            total += prof_bonus // 2
        skills[readable(slug)] = {
            "ability": ability,
            "total": total,
            "proficient": slug in proficient,
            "expertise": slug in expertise,
        }

    senses = {}
    for e in entries:
        if e.get("type") in ("set", "set-base") and e.get("subType") in SENSE_SUBTYPES:
            amount = modifier_amount(e)
            if isinstance(amount, (int, float)):
                senses[e["subType"]] = max(amount, senses.get(e["subType"], 0))

    other_profs = sorted(
        readable(s) for s in proficient
        if s not in SKILL_ABILITIES and not s.endswith("-saving-throws")
        and not s.startswith("choose-")
    )
    languages = sorted(readable(s) for s in subtypes("language"))

    hit_dice = []
    for cls in data.get("classes", []):
        definition = cls.get("definition") or {}
        die = definition.get("hitDice")
        level = cls.get("level") or 0
        used = cls.get("hitDiceUsed") or 0
        if die and level:
            hit_dice.append({
                "class": definition.get("name"),
                "die": "d" + str(die),
                "total": level,
                "used": used,
                "remaining": level - used,
            })

    # Initiative: flat bonuses sum normally, but the Alert feat's bonus
    # is stored with no amount at all, because the amount is the
    # proficiency bonus. An amount-less initiative bonus therefore
    # counts as one proficiency bonus each.
    initiative = ability_mods["dexterity"]
    for e in entries:
        if e.get("type") == "bonus" and e.get("subType") == "initiative":
            if e.get("fixedValue") is None and e.get("value") is None:
                initiative += prof_bonus
            else:
                initiative += modifier_amount(e)

    return {
        "saving_throws": saving_throws,
        "skills": skills,
        "initiative": initiative,
        "passive_perception": 10 + skills["Perception"]["total"],
        "proficiencies": other_profs,
        "languages": languages,
        "senses": senses,
        "hit_dice": hit_dice,
        "xp": data.get("currentXp"),
        "inspiration": bool(data.get("inspiration")),
        "proficiency_subtypes": proficient,
    }


def find_dice_string(obj):
    """Return the first diceString found anywhere inside a spell entry."""
    if isinstance(obj, dict):
        if obj.get("diceString"):
            return obj["diceString"]
        for value in obj.values():
            found = find_dice_string(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_dice_string(value)
            if found:
                return found
    return None


def estimate_ac(items, dex_mod, unarmored_stat_mod=None, flat_bonus=0, set_base=None):
    """Calculate AC from equipped armour and shield, or from unarmoured
    defence when the character has it and wears no armour. D&D Beyond
    applies conditional bonuses this cannot see, so verify it."""
    armor = next((i for i in items if i["equipped"] and "base_armor_class" in i
                  and i.get("armor_type") != "shield"), None)
    shield = next((i for i in items if i["equipped"] and i.get("armor_type") == "shield"), None)
    if armor:
        base = armor["base_armor_class"]
        if armor["armor_type"] == "light":
            ac = base + dex_mod
        elif armor["armor_type"] == "medium":
            ac = base + min(dex_mod, 2)
        elif armor["armor_type"] == "heavy":
            ac = base
        else:
            ac = base + dex_mod
    elif set_base is not None:
        ac = set_base + dex_mod
    else:
        ac = 10 + dex_mod + (unarmored_stat_mod or 0)
    ac += flat_bonus
    shield_name = None
    if shield:
        ac += shield.get("base_armor_class") or 2
        shield_name = shield["name"]
    return ac, (armor["name"] if armor else None), shield_name


def convert(data):
    warnings = []
    scores = ability_scores(data)
    mods = {k: modifier(v) for k, v in scores.items()}
    class_text, total_level = class_summary(data)
    prof_bonus = 2 + (max(total_level, 1) - 1) // 4

    items = inventory_items(data)
    for item in items:
        if "base_armor_class" in item and not item["equipped"]:
            warnings.append(
                f"{item['name']} is in the inventory but equipped = false, so it does NOT "
                f"count toward armour class. If this character should be wearing it, equip it "
                f"in D&D Beyond (Inventory, tick the Active / Equip box for the item), then "
                f"export again. If they are carrying it as spare gear, ignore this warning."
            )

    flat_ac_bonus = sum(modifier_amount(e) for e in iter_modifiers(data)
                        if e.get("subType") == "armor-class" and e.get("type") == "bonus")
    unarmored_stat_mod = None
    set_ac_base = None
    for entry in iter_modifiers(data):
        if entry.get("subType") == "unarmored-armor-class" and entry.get("type") == "set":
            ability = ABILITY_IDS.get(entry.get("statId"))
            if ability:
                unarmored_stat_mod = mods[ability]
        if entry.get("subType") == "armor-class" and entry.get("type") == "set":
            set_ac_base = modifier_amount(entry)
    armor_class, armor_name, shield_name = estimate_ac(
        items, mods["dexterity"], unarmored_stat_mod, flat_ac_bonus, set_ac_base)
    if not armor_name and unarmored_stat_mod is None and set_ac_base is None:
        warnings.append("No equipped armour found and no unarmoured defence in the modifier list, AC uses the plain unarmoured formula, 10 + dexterity modifier.")

    base_hp = data.get("baseHitPoints")
    removed_hp = data.get("removedHitPoints") or 0
    # Temp HP is always 0 in the clean starting state, by design (Sophie,
    # 2026-11-10). Characters begin the benchmark fresh, and scenarios that
    # need temp HP set it in their own starting state. Revisit if the
    # benchmark later adds temp HP granting effects.
    temp_hp = 0
    hp_max = None
    hp_current = None
    if base_hp is not None:
        per_level_hp = sum(modifier_amount(e) for e in iter_modifiers(data)
                           if e.get("subType") == "hit-points-per-level" and e.get("type") == "bonus")
        flat_hp = sum(modifier_amount(e) for e in iter_modifiers(data)
                      if e.get("subType") == "hit-points" and e.get("type") == "bonus")
        if data.get("overrideHitPoints") is not None:
            hp_max = data["overrideHitPoints"]
        else:
            hp_max = (base_hp + mods["constitution"] * max(total_level, 1)
                      + per_level_hp * max(total_level, 1) + flat_hp
                      + (data.get("bonusHitPoints") or 0))
        hp_current = hp_max - removed_hp

    currencies = data.get("currencies") or {}
    conditions = [CONDITION_IDS.get(c.get("id"), f"condition_{c.get('id')}")
                  for c in data.get("conditions", [])]
    death = data.get("deathSaves") or {}

    # Spellcasting numbers. The class definition in the export names the
    # casting ability (spellCastingAbilityId) and carries the class spell
    # slot table (spellRules.levelSpellSlots, one row per class level).
    # Spell attack and save DC are not stored anywhere, but they are a
    # fixed formula: casting ability modifier + proficiency bonus, and
    # DC = 8 + that attack bonus.
    casting_classes = []
    for cls in data.get("classes", []):
        cdef = cls.get("definition") or {}
        if cdef.get("canCastSpells") and cdef.get("spellCastingAbilityId"):
            casting_classes.append((cls, cdef))
    spellcasting = None
    if casting_classes:
        blocks = []
        for cls, cdef in casting_classes:
            ability = ABILITY_IDS.get(cdef.get("spellCastingAbilityId"))
            if not ability:
                continue
            attack = mods[ability] + prof_bonus
            blocks.append({
                "class": cdef.get("name"),
                "ability": ability,
                "ability_modifier": mods[ability],
                "spell_attack_bonus": attack,
                "save_dc": 8 + attack,
            })
        if len(blocks) == 1:
            spellcasting = blocks[0]
        elif blocks:
            spellcasting = blocks
    slots_max = None
    slots_remaining = None
    if casting_classes:
        effective_level = 0
        slot_table = None
        for cls, cdef in casting_classes:
            rules = cdef.get("spellRules") or {}
            divisor = rules.get("multiClassSpellSlotDivisor") or 1
            effective_level += (cls.get("level") or 0) // divisor
            if slot_table is None and rules.get("levelSpellSlots"):
                slot_table = rules["levelSpellSlots"]
        if slot_table and 0 < effective_level < len(slot_table):
            row = slot_table[effective_level]
            slots_max = {str(i + 1): n for i, n in enumerate(row) if n}
            used_by_level = {s.get("level"): s for s in data.get("spellSlots") or []}
            slots_remaining = {}
            for lvl, maximum in slots_max.items():
                record = used_by_level.get(int(lvl)) or {}
                available = record.get("available") or maximum
                slots_remaining[lvl] = available - (record.get("used") or 0)

    # Spells live in several places in the raw export: classSpells (a list
    # of per class objects, each with its own spells list), and a grouped
    # "spells" object with race, class, background, item, and feat lists.
    # The grouped object is easy to miss and it matters: subclass granted
    # spells (an Evoker's Savant picks) sit in spells.class, and feat
    # spells (Magic Initiate) sit in spells.feat, neither appears in
    # classSpells. Each entry carries prepared and alwaysPrepared flags,
    # and its definition carries the spell level (0 means cantrip).
    # D&D Beyond has no prepared only export, so the split into castable
    # vs spellbook only has to happen here, after export.
    def iter_spell_entries():
        for entry in data.get("classSpells", []) or []:
            for spell in entry.get("spells", []) or []:
                yield spell, "classSpells"
        grouped = data.get("spells")
        if isinstance(grouped, dict):
            for group, lst in grouped.items():
                if isinstance(lst, list):
                    for spell in lst:
                        if isinstance(spell, dict) and "definition" in spell:
                            yield spell, f"spells.{group}"
        for key in ("raceSpells", "backgroundSpells", "featSpells"):
            value = data.get(key)
            if isinstance(value, list):
                for spell in value:
                    if isinstance(spell, dict) and "definition" in spell:
                        yield spell, key

    # Only a Wizard keeps a spellbook with spells in it that are not
    # prepared. Every other casting class's class spell list simply is
    # their prepared (or known) set, and D&D Beyond leaves the
    # prepared flag false on all of them, so flag based sorting would
    # empty a Bard's or a Sorcerer's list entirely.
    has_spellbook = any((cdef.get("name") == "Wizard")
                        for _, cdef in casting_classes)
    cantrips = []
    spells_prepared = []
    spells_always_prepared = []
    spells_unprepared = []
    spell_details = []
    seen = set()
    for spell, source in iter_spell_entries():
        definition = spell.get("definition") or {}
        name = definition.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        level = definition.get("level")
        # Cantrips are never prepared, they are simply known and can
        # always be used, so they are marked always prepared here.
        # Leveled spells granted by a feat, species, or background are
        # also always prepared under the 2024 rules (Magic Initiate is
        # the common case), even though the export leaves their flags false.
        granted_always = source.startswith("spells.") and source not in ("spells.class", "spells.item")
        always_prepared = (bool(spell.get("alwaysPrepared")) or level == 0
                           or (granted_always and (level or 0) >= 1))
        # How this spell is resolved: attack roll, saving throw, or neither.
        ability_id = spell.get("spellCastingAbilityId")
        if not ability_id and casting_classes:
            ability_id = casting_classes[0][1].get("spellCastingAbilityId")
        ability_name = ABILITY_IDS.get(ability_id)
        if definition.get("requiresAttackRoll") and ability_name:
            resolution = {"type": "attack_roll", "bonus": mods[ability_name] + prof_bonus}
        elif definition.get("requiresSavingThrow"):
            dc = spell.get("overrideSaveDc")
            if dc is None and ability_name:
                dc = 8 + mods[ability_name] + prof_bonus
            resolution = {"type": "saving_throw",
                          "ability": ABILITY_IDS.get(definition.get("saveDcAbilityId")),
                          "dc": dc}
        else:
            resolution = {"type": "none"}
        # Damage: a damage modifier in the definition names the type but
        # rarely carries dice, so dice usually come from the description.
        damage_modifier = next((m for m in definition.get("modifiers") or []
                                if m.get("type") == "damage"), None)
        damage_type = None
        damage_dice = None
        damage_source = None
        if damage_modifier:
            damage_type = (damage_modifier.get("subType") or "").capitalize() or None
            dice = (damage_modifier.get("dice") or {}).get("diceString")
            if dice:
                damage_dice, damage_source = dice, "spell data"
        if not damage_dice:
            from_text = description_damage(definition)
            if from_text:
                damage_dice, damage_source = from_text, "spell description text"
        healing_dice = next(((h or {}).get("diceString") for h in definition.get("healingDice") or []
                             if isinstance(h, dict) and h.get("diceString")), None)
        spell_details.append({
            "name": name,
            "level": level,
            "school": definition.get("school"),
            "prepared": bool(spell.get("prepared") or always_prepared),
            "always_prepared": always_prepared,
            "ritual": bool(definition.get("ritual")),
            "concentration": bool(definition.get("concentration")),
            "casting_time": casting_time_text(definition, spell),
            "range": range_text(spell.get("range") or definition.get("range")),
            "duration": duration_text(definition.get("duration")),
            "components": ", ".join(COMPONENT_IDS[c] for c in definition.get("components") or []
                                    if c in COMPONENT_IDS) or None,
            "resolution": resolution,
            "damage_dice": damage_dice,
            "damage_type": damage_type,
            "damage_source": damage_source,
            "healing_dice": healing_dice,
            "scale_type": definition.get("scaleType"),
            "source": source,
        })
        if level == 0:
            cantrips.append(name)
        elif always_prepared:
            spells_always_prepared.append(name)
        elif not has_spellbook and source in ("classSpells", "spells.class"):
            # D&D Beyond leaves "prepared" false on these, so set it here
            # to match spells_prepared and keep the sheet from contradicting itself.
            spells_prepared.append(name)
            spell_details[-1]["prepared"] = True
        elif spell.get("prepared"):
            spells_prepared.append(name)
        else:
            # Plain names everywhere, so a scenario can match them exactly.
            # Whether a spell is a ritual lives in spell_details.
            spells_unprepared.append(name)

    derived = extract_derived(data, mods, prof_bonus)
    attacks = weapon_attacks(data, mods, prof_bonus, derived["proficiency_subtypes"])
    feature_data = extract_features(data, prof_bonus, mods, scores)

    race = data.get("race") or {}
    # The picks a player made are not listed anywhere in the clean file
    # as choices. Their results simply are the character: picked skills
    # are the proficient skills, picked languages are the languages,
    # picked spells are the spell lists. The one pick whose result has
    # no other home is a species level pick like a dragonborn's
    # ancestry, so that one is folded into the species line itself.
    species_name = race.get("fullName") or race.get("baseName")
    # An ancestry pick (a dragonborn's dragon, an elf's lineage) is
    # identified structurally, not by guessing from its name: the
    # choice that records it points at the Ancestry or Lineage trait
    # in the species trait list, through the component id both share.
    ancestry_components = set()
    for trait in race.get("racialTraits") or []:
        tdef = (trait or {}).get("definition") or {}
        tname = tdef.get("name") or ""
        if "Ancestry" in tname or tname.endswith("Lineage"):
            ancestry_components.add(tdef.get("id"))
    for pick in extract_choices(data):
        if pick["source"] == "race" \
                and pick.get("componentId") in ancestry_components:
            species_name = f"{species_name} ({pick['picked']})"
            break
    clean = {
        "name": data.get("name"),
        "species": species_name,
        "class": class_text,
        "level": total_level,
        "abilities": scores,
        "ability_modifiers": mods,
        "proficiency_bonus": prof_bonus,
        "hp_current": hp_current,
        "hp_max": hp_max,
        "temp_hp": temp_hp,
        "armor_class": armor_class,
        "equipped_armor": armor_name,
        "equipped_shield": shield_name,
        "conditions": conditions,
        "death_saves": {
            "successes": death.get("successCount") or 0,
            "failures": death.get("failCount") or 0,
        },
        "currencies": currencies,
        "gold": currencies.get("gp", 0),
        "inventory": items,
        "cantrips_known": sorted(cantrips),
        "spells_prepared": sorted(spells_prepared),
        "spells_always_prepared": sorted(spells_always_prepared),
        "spells_in_spellbook_not_prepared": sorted(spells_unprepared),
        "attacks": attacks,
        "saving_throws": derived["saving_throws"],
        "skills": derived["skills"],
        "initiative": derived["initiative"],
        "passive_perception": derived["passive_perception"],
        "proficiencies": derived["proficiencies"],
        "languages": derived["languages"],
        "senses": derived["senses"],
        "hit_dice": derived["hit_dice"],
        "xp": derived["xp"],
        "inspiration": derived["inspiration"],
        "speed_ft": feature_data["speed_ft"],
        "resistances": feature_data["resistances"],
        "vulnerabilities": feature_data["vulnerabilities"],
        "immunities": feature_data["immunities"],
        "features": feature_data["features"],
        "feature_uses": feature_data["feature_uses"],
        "class_features": feature_data["class_features"],
        "species_traits": feature_data["species_traits"],
        "feats": feature_data["feats"],
        "spellcasting": spellcasting,
        "spell_details": sorted(spell_details, key=lambda s: (s["level"] if s["level"] is not None else 99, s["name"])),
        "spell_slots_remaining": slots_remaining,
        "spell_slots_max": slots_max,
    }
    return clean, warnings


def main():
    parser = argparse.ArgumentParser(description="Convert a D&D Beyond raw JSON export to a clean Faithful DM state")
    parser.add_argument("input", help="Raw D&D Beyond JSON file")
    parser.add_argument("-o", "--output", help="Output file, defaults to printing to screen")
    args = parser.parse_args()

    try:
        data = load_character(args.input)
    except Exception as exc:
        print(f"Could not read that file as a D&D Beyond character export: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or "name" not in data:
        print("That file does not look like a D&D Beyond character export, no character name found in it.", file=sys.stderr)
        sys.exit(1)

    clean, warnings = convert(data)
    text = json.dumps(clean, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"Wrote {args.output}")
        for warning in warnings:
            print(f"WARNING: {warning}")
    else:
        print(text)


if __name__ == "__main__":
    main()
