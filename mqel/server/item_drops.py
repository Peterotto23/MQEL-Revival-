"""Ausrüstungs-Drops für PVE-Angriffe aus den GameplaySettings des Clients.

Belegt aus den Daten:
- Anzahl Drops je Burg: AttackerRewardSettings.ItemDropByType.User
  .ItemDropCountTable (nach Summe der Kreatur-ConstructionPoints).
- Item-Level: Burg-Level +-1 nach ItemLevelTable, höchstens Helden-Level
  (wie HeroInventoryLootItemHelper.GenerateItemLevel).
- Für welchen Helden: EquipmentGenerationSettings.ItemClassTable
  (gespielter Held 70 %, auf niedrigem Level mehr, sonst einer der anderen;
  GenerateItemClass), Schatzkammer TreasureRoomItemClassTable.
- Schatzkammer-Item mit Wahrscheinlichkeit nach Burg-Schwierigkeit
  (TreasureRoomItemDropProbabilityTable).
- Item-Typ: Heroes/<Held>/Gameplay.json HeroEquipmentDefinitionSpec
  .HeroItemTypes (RandomChance als Gewicht; ohne Chance = nicht droppbar).
- Qualität = Anzahl Effekte (EquipmentCategorySettings: Magic 1 ...
  Epic 4); Mindestqualität Ring/Amulett = Magic.
- Named-Items (feste Effekte) nur bei Legendary/Epic mit RarityLootTable.
- Effekte: HeroItemCategories.AllowedEffects, magische Eigenschaften nach
  MinItemQuality und RandomChance, Level nach NamedItemEffectLevelUnlock.
- Max. Items je Kreatur: MaxItemDropPerCreatureRank.

Annahmen (Server-Logik unbekannt):
- Qualitätswurf je Item unabhängig mit p = QualityStepModifier/Frequenz
  statt des zustandsbehafteten "SmartLoot"-Zählers des Originals.
- Item-Fähigkeiten (Effekt-Typ Ability) mit festem Gewicht ABILITY_CHANCE.
- PrimaryStatsModifiers gleichverteilt 0.8..1.0.
"""

import copy
import functools
import glob
import json
import os
import random

import castles
import loot

T = castles.T
SETTINGS = loot.SETTINGS

QUALITIES = ("Basic", "Magic", "Exceptional", "Legendary", "Epic")
ABILITY_CHANCE = 0.03
# Archetypen, die nicht droppen (HeroItemArchetypesSettings); Waffen und
# Rüstungen haben getrennte Id-Räume.
EXCLUDED_WEAPON_ARCHETYPES = {13}            # Generous Unknown Weapon
EXCLUDED_ARMOR_ARCHETYPES = {5, 7, 8, 9}     # Generous Named, Shop, Default, Converted
TYPE_ROLLS = 5  # Typen ohne Template auf diesem Level (Amulett < 6) neu würfeln
EFFECT_TYPE = {"Ability": 1}  # sonst 0 (magische Eigenschaft)


def _load(*parts):
    with open(os.path.join(SETTINGS, *parts), encoding="utf-8") as f:
        return json.load(f)


@functools.cache
def _gen():
    return _load("HeroItems", "EquipmentGenerationSettings.json")


@functools.cache
def _templates():
    return _load("HeroItems", "HeroItemTemplates.json")["TemplateList"]


@functools.cache
def _item_types():
    return {t["Id"]: t for t in _load("HeroItems", "HeroItemTypes.json")["ItemTypeList"]}


@functools.cache
def _category_effects():
    """CategoryType ("Ring" für den Eintrag ohne Typ) -> AllowedEffects."""
    return {c.get("CategoryType", "Ring"): c.get("AllowedEffects", [])
            for c in _load("HeroItems", "HeroItemCategories.json")["CategoryList"]}


@functools.cache
def _effect_counts():
    cats = _load("HeroItems", "EquipmentCategorySettings.json")["EquipmentCategories"]
    return {q: cats[q].get("EffectCount", 0) for q in QUALITIES}


@functools.cache
def _magical_properties():
    return {p["Id"]: p for p in _gen()["MagicalProperties"]}


@functools.cache
def hero_item_types(hero_spec):
    """[(HeroItemTypeId, Gewicht)] der droppbaren Typen eines Helden."""
    path = glob.glob(os.path.join(SETTINGS, "Heroes", f"{hero_spec:06d} - *",
                                  "Gameplay.json"))
    if not path:
        return ()
    with open(path[0], encoding="utf-8") as f:
        for comp in json.load(f):
            if comp["$type"].split(",")[0].endswith(".HeroEquipmentDefinitionSpec"):
                return tuple((t["HeroItemTypeId"], t["RandomChance"])
                             for t in comp["HeroItemTypes"] if "RandomChance" in t)
    return ()


def _category(type_id):
    return _item_types()[type_id].get("HeroItemCategoryType", "Ring")


def _weighted(pairs):
    pairs = [p for p in pairs if p[1] > 0]
    if not pairs:
        return None
    return random.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _drop_settings():
    return loot.rewards()["ItemDropByType"]["User"][0]


def roll_quality(castle_level, category, magic_find=0):
    """magic_find: Zusatzfaktor (Magic-Find-Boost 1 = doppelte Chance je
    Qualitätsstufe)."""
    s = _drop_settings()
    mods = next((r["QualityStepModifier"]
                 for r in s["ItemQualityStepModifiersByLevelRanges"]
                 if r["Min"] <= castle_level <= r["Max"]), {})
    quality = "Basic"
    for q in ("Epic", "Legendary", "Exceptional", "Magic"):
        if random.random() < (mods.get(q, 0) * (1 + magic_find)
                              / s["ItemQualityFrequency"][q]):
            quality = q
            break
    minimum = _gen()["MinimumQualityByCategory"].get(category)
    if minimum and QUALITIES.index(quality) < QUALITIES.index(minimum):
        quality = minimum
    return quality


def roll_item_level(castle_level, table_key="ItemLevelTable", hero_level=30):
    table = _drop_settings()[table_key]
    diff = _weighted([(-1, table["Minus1Level"]), (0, table["SameLevel"]),
                      (1, table["Plus1Level"])])
    return max(1, min(30, hero_level, castle_level + diff))


def _effect_level(item_level):
    # Alle Eigenschaften nutzen dieselbe Freischalt-Tabelle
    unlock = next(iter(_magical_properties().values()))["NamedItemEffectLevelUnlock"]
    return max(u["MaxEffectLevel"] for u in unlock if u["MinItemLevel"] <= item_level)


def _effect(effect_id, effect_type, level):
    return {"$type": T("ItemEffect"), "Id": effect_id, "Level": level,
            "Type": effect_type}


def _random_effects(category, quality, item_level):
    props = _magical_properties()
    q = QUALITIES.index(quality)
    pool = []
    for e in _category_effects().get(category, []):
        if e.get("Type") == "Ability":
            pool.append(((e["Id"], 1), ABILITY_CHANCE))
        elif e["Id"] in props:
            p = props[e["Id"]]
            if QUALITIES.index(p.get("MinItemQuality", "Magic")) <= q:
                pool.append(((e["Id"], 0), p.get("RandomChance", 0)))
    level = _effect_level(item_level)
    out = []
    for _ in range(_effect_counts()[quality]):
        pick = _weighted(pool)
        if pick is None:
            break
        pool = [p for p in pool if p[0] != pick]  # jede Eigenschaft einmal
        out.append(_effect(pick[0], pick[1], level))
    return out


def _fits(t, type_id, level):
    excluded = (EXCLUDED_WEAPON_ARCHETYPES if _category(type_id) == "Weapon"
                else EXCLUDED_ARMOR_ARCHETYPES)
    return (t["HeroItemTypeId"] == type_id
            and t["LevelMin"] <= level <= t["LevelMax"]
            and t.get("ArchetypeId") not in excluded)


def roll_item(hero_spec, castle_level, table_key="ItemLevelTable", magic_find=0,
              hero_level=30):
    """Ein HeroEquipmentItem in Client-Form, oder None."""
    level = roll_item_level(castle_level, table_key, hero_level)
    for _ in range(TYPE_ROLLS):
        item = _roll_typed(hero_spec, castle_level, level, magic_find)
        if item:
            return item
    return None


def _roll_typed(hero_spec, castle_level, level, magic_find=0, quality=None,
                type_id=None):
    type_id = type_id or _weighted(hero_item_types(hero_spec))
    if type_id is None:
        return None
    category = _category(type_id)
    if quality is None:
        quality = roll_quality(castle_level, category, magic_find)

    named_chance = _gen()["RarityLootTable"][quality].get("Named", 0)
    if random.random() < named_chance:
        named = [t for t in _templates()
                 if t.get("Rarity") == "Named" and t.get("IsDroppable")
                 and _fits(t, type_id, level)]
        if named:
            t = random.choice(named)
            elevel = _effect_level(level)
            return _item(t, level, [
                _effect(e["Id"], EFFECT_TYPE.get(e.get("Type"), 0), elevel)
                for e in t.get("Effects", [])])

    common = [t for t in _templates()
              if t.get("Rarity") != "Named" and _fits(t, type_id, level)]
    if not common:
        return None
    t = random.choice(common)
    return _item(t, level, _random_effects(category, quality, level))


def forge_item(hero_spec, level, quality):
    """Schmiede: Item fester Qualität und festen Levels, oder None."""
    for _ in range(TYPE_ROLLS):
        item = _roll_typed(hero_spec, level, level, quality=quality)
        if item:
            return item
    return None


# REFORGE_PATCH_V3
print("[reforge] patch v3 active, item_drops loaded from " + os.path.abspath(__file__), flush=True)

REFORGE_SLOTS = ("Weapon", "Helm", "ShoulderArmor", "SuitOfArmor", "Gloves", "Capes")


def reforge_item(source, level, quality, hero=None, keep_slot=False):
    """Umschmieden: gleiche Qualitaet, NEUES Item fuer den gespielten Helden `hero`,
    Slot zufaellig (Waffe/Helm/Schultern/Brust/Handschuhe/Umhang), nie derselbe Slot wie
    `source`; Ringe und Amulette kommen nie heraus. `keep_slot` wird ignoriert.
    Pets bleiben Pets. Ohne Held oder wenn nichts passt: gleicher Typ wie `source`."""
    src_type = _templates_by_id()[source["TemplateId"]]["HeroItemTypeId"]
    src_cat = _category(src_type)
    own = hero_item_types(hero) if hero else ()
    if not own or src_cat == "Pet":
        return _reforge_same_type(source, level, quality, src_type)
    by_cat = {}
    for t, _w in own:
        if _category(t) in REFORGE_SLOTS:
            by_cat.setdefault(_category(t), []).append(t)
    cats = [c for c in REFORGE_SLOTS if c in by_cat and c != src_cat]
    random.shuffle(cats)
    fallback = None
    for cat in cats:
        for type_id in by_cat[cat]:
            for _ in range(TYPE_ROLLS):
                item = _roll_typed(None, level, level, quality=quality, type_id=type_id)
                if item and item["TemplateId"] != source["TemplateId"]:
                    return item
                fallback = fallback or item
    return fallback or _reforge_same_type(source, level, quality, src_type)


def _reforge_same_type(source, level, quality, type_id):
    """Altes Verhalten: gleicher Item-Typ und Archetyp wie `source`."""
    item = _roll_typed(None, level, level, quality=quality, type_id=type_id)
    if item is None:  # Typen nur mit Named-Templates (z. B. Pets)
        named = [t for t in _templates() if t.get("Rarity") == "Named"
                 and _fits(t, type_id, level)]
        if named:
            t = random.choice(named)
            item = _item(t, level, [_effect(e["Id"], EFFECT_TYPE.get(e.get("Type"), 0),
                                            _effect_level(level))
                                    for e in t.get("Effects", [])])
    if item and source.get("ArchetypeId"):
        item["ArchetypeId"] = source["ArchetypeId"]
    return item


def item_quality(item):
    """Qualität eines Teils: steckt in der Zahl seiner Effekte
    (_effect_counts, so würfelt _random_effects); höchste passende."""
    n = len(item.get("Effects") or [])
    counts = _effect_counts()
    return max((q for q in QUALITIES if counts.get(q, 0) <= n),
               key=QUALITIES.index, default="Basic")


def upgrade_item(source, level, chances):
    """Aufwerten: dasselbe Teil auf `level`; jede magische Eigenschaft
    (Type 0) steigt mit ForgeSettings.PropertyLevelUpChances
    [{LevelFrom, LevelTo, PropertyLevel, Chance}] je nach neuem Level und
    ihrem bisherigen Level um eine Stufe."""
    item = copy.deepcopy(source)
    item["ItemLevel"] = level
    for effect in item.get("Effects") or []:
        if effect.get("Type") != 0:
            continue
        chance = next((c["Chance"] for c in chances
                       if c["LevelFrom"] <= level <= c["LevelTo"]
                       and c["PropertyLevel"] == effect["Level"]), 0)
        if random.random() < chance:
            effect["Level"] += 1
    return item


@functools.cache
def _templates_by_id():
    return {t["Id"]: t for t in _templates()}


def _item(t, level, effects):
    # Rüstungs-Templates tragen keinen Archetyp; er wird je Item gewürfelt
    archetype = t.get("ArchetypeId") or random.randint(1, 4)
    return {
        "$type": T("HeroEquipmentItem"),
        "ArchetypeId": archetype,
        "DyeTemplateId": 0,
        "Effects": effects,
        "IsBranded": False,
        "IsSellable": True,
        "ItemLevel": level,
        "PrimaryStatsModifiers": [round(random.uniform(0.8, 1.0), 3)
                                  for _ in range(3)],
        "TemplateId": t["Id"],
    }


def _item_hero(played_hero, hero_level, table=None):
    """Held, für den ein Item droppt (GenerateItemClass): gespielter Held
    mit HeroPlayedByUser, bis Level OtherHeroes/BonusPerLowLevel je Level
    darunter BonusPerLowLevel mehr; sonst einer der anderen."""
    table = table or _gen()["ItemClassTable"]
    chance = table["HeroPlayedByUser"]
    if table.get("BonusPerLowLevel"):
        threshold = int(round(table["OtherHeroes"] / table["BonusPerLowLevel"], 6))  # 0.3/0.05 = 6
        if hero_level <= threshold:
            chance += (threshold - hero_level + 1) * table["BonusPerLowLevel"]
    if random.random() < chance:
        return played_hero
    table = _gen()["ItemClassTable"]
    others = [h for h in table["LootableHeroSpecIds"] if h != played_hero]
    return random.choice(others) if others else played_hero


def creature_items(castle, played_hero, magic_find=0, hero_level=30):
    """{Entity-Id: [Items]} für die Kreaturen der Burg; magic_find hebt die
    Qualität (laut Spieltext nur bei Drops besiegter Kreaturen)."""
    creatures = [(c["Id"],) + loot.creature(c["SpecContainerId"])[0:1]
                 + (loot.creature_rank(c["SpecContainerId"]),)
                 for room in castle.get("Rooms") or []
                 for c in room.get("Creatures") or []]
    total_cp = sum(cp for _, cp, _ in creatures)
    rows = [r for r in _drop_settings()["ItemDropCountTable"]
            if total_cp >= r.get("MinCP", 0)]
    probs = rows[-1]["ItemDropProbabilities"]
    count = _weighted([(p["DropCount"], p["Probability"]) for p in probs]) or 0

    max_per_rank = loot.rewards()["MaxItemDropPerCreatureRank"]
    capacity = {eid: max_per_rank.get(rank, 1) for eid, _, rank in creatures}
    weights = {eid: max(cp, 1) for eid, cp, _ in creatures}
    out = {}
    for _ in range(count):
        eid = _weighted([(e, w) for e, w in weights.items() if capacity[e] > 0])
        if eid is None:
            break
        item = roll_item(_item_hero(played_hero, hero_level), castle["Level"],
                         magic_find=magic_find, hero_level=hero_level)
        if item:
            out.setdefault(eid, []).append(item)
            capacity[eid] -= 1
    return out


def treasure_item(castle, played_hero, hero_level):
    """Item der Schatzkammer oder None (GenerateTreasureRoomItemWithProbability)."""
    names = {v: k for k, v in castles.DIFFICULTY.items()}
    difficulty = names[castles.difficulty(castle["Level"], hero_level)]
    if random.random() >= loot.rewards()["TreasureRoomItemDropProbabilityTable"][difficulty]:
        return None
    hero = _item_hero(played_hero, hero_level, loot.rewards()["TreasureRoomItemClassTable"])
    return roll_item(hero, castle["Level"], "TreasureRoomItemLevelTable",
                     hero_level=hero_level)


@functools.cache
def event_templates(event):
    """Template-Ids der Event-Gegenstände: SKUs "Event <Event>: ..." im
    Katalog (z. B. "Event Halloween: Ring 10"), alle ohne Preis und nie
    aktiv; wie das Original sie vergab, ist unbekannt."""
    skus = _load("ShopSettings", "ShopSkuBaseSettings.json")["Skus"]
    prefix = f"event {event.lower()}:"
    return frozenset(x["ItemId"] for x in skus if x.get("ItemType") == 3
                     and x.get("InternalDescription", "").lower().startswith(prefix))


def event_treasure_item(event, castle_level, played_hero):
    """Schatzkammer-Item einer Saison-Burg: ein Event-Gegenstand, der zur
    Klasse des Helden passt; je Gegenstandstyp (Ring, Waffe) der mit dem
    höchsten Level bis zum Burglevel, daraus einer zufällig. None, wenn
    keiner passt."""
    types = {t for t, _ in hero_item_types(played_hero)}
    best = {}
    for t in _templates():
        if (t["Id"] not in event_templates(event) or t["HeroItemTypeId"] not in types
                or t["LevelMin"] > castle_level):
            continue
        cur = best.get(t["HeroItemTypeId"])
        if cur is None or t["LevelMin"] > cur["LevelMin"]:
            best[t["HeroItemTypeId"]] = t
    if not best:
        return None
    t = random.choice(list(best.values()))
    level = t["LevelMin"]
    return _item(t, level, [
        _effect(e["Id"], EFFECT_TYPE.get(e.get("Type"), 0), _effect_level(level))
        for e in t.get("Effects", [])])


# -- Magische Eigenschaften der angelegten Ausrüstung ------------------------
# Wert je Effekt = BaseValue + ValuePerLevel * (Level - 1), über alle
# angelegten Teile summiert.

MAGIC_FIND = 26
# Effekt -> (Zutaten-Qualität, Anzahl, nur Spielerburgen); Texte 15818-15821,
# 16158 ("per player castle attack"); None = Zutat beliebiger Qualität
BONUS_MATERIALS = {40: ("Basic", 3, False), 41: ("Exceptional", 1, False),
                   42: ("Legendary", 1, False), 43: ("Epic", 1, False),
                   48: (None, 1, True)}


def hero_effect(hero, effect_id):
    prop = _magical_properties()[effect_id]
    return sum(prop["BaseValue"] + prop.get("ValuePerLevel", 0) * (e["Level"] - 1)
               for item in (hero.get("Equipment") or {}).values() if item
               for e in item.get("Effects") or []
               if e["Id"] == effect_id and e.get("Type", 0) == 0)


@functools.cache
@functools.cache
def _lootable_consumables(kind):
    """Qualität -> lootbare Verbrauchsgüter des Template-Typs kind
    (CraftingMaterial, Dye); ohne Qualität gilt Basic."""
    out = {}
    for t in _load("GeneralSettings", "ConsumableSettings.json")["TemplateList"]:
        if (t["$type"].split(",")[0].endswith(f".{kind}ConsumableTemplate")
                and t.get("IsLootable")):
            out.setdefault(t.get("Quality", "Basic"), []).append(t["TemplateId"])
    return out


def _materials():
    """Qualität -> lootbare Zutaten (CraftingMaterialConsumableTemplate)."""
    return _lootable_consumables("CraftingMaterial")


# ConsumableCategoryTable -> Template-Typ
CONSUMABLE_KINDS = {"Crafting": "CraftingMaterial", "Dye": "Dye"}


def creature_consumables(castle, attacker_castle_level):
    """Verbrauchsgüter-Drops der Kreaturen einer Spielerburg
    (AttackerRewardSettings.ItemDropByType.User: ConsumableDropCountTable
    nach den Bau-Punkten der Kreaturen, ConsumableDropCountModifer nach dem
    Burgstufen-Vorsprung des Angreifers, Kategorie nach
    ConsumableCategoryTable, Qualität je Stufe mit 1/ConsumableQualityFrequency).
    {Entity-Id: [HeroConsumableItem]}"""
    s = _drop_settings()
    creatures = {c["Id"]: max(loot.creature(c["SpecContainerId"])[0], 1)
                 for room in castle.get("Rooms") or []
                 for c in room.get("Creatures") or []}
    if not creatures:
        return {}
    total_cp = sum(creatures.values())
    rows = [r for r in s["ConsumableDropCountTable"] if total_cp >= r.get("MinCP", 0)]
    count = _weighted([(p.get("DropCount", 0), p["Probability"])
                       for p in rows[-1]["ItemDropProbabilities"]]) or 0
    lead = max(attacker_castle_level - castle.get("Level", 1), 0)
    modifier = [r.get("DropModifier", 0) for r in s["ConsumableDropCountModifer"]
                if lead >= r.get("AttackerCastleLevelDiff", 0)][-1]
    out = {}
    for _ in range(count):
        if random.random() >= modifier:
            continue
        category = _weighted(list(s["ConsumableCategoryTable"].items()))
        kind = CONSUMABLE_KINDS.get(category) if category is not None else None
        quality = next((q for q in ("Epic", "Legendary", "Exceptional", "Magic")
                        if s["ConsumableQualityFrequency"].get(q)
                        and random.random() < s["ConsumableStepBase"]
                        / s["ConsumableQualityFrequency"][q]), "Basic")
        pools = _lootable_consumables(kind) if kind else {}
        pool = pools.get(quality) or [t for ts in pools.values() for t in ts]
        if pool:
            eid = _weighted(list(creatures.items()))
            out.setdefault(eid, []).append(
                {"$type": T("HeroConsumableItem"), "TemplateId": random.choice(pool)})
    return out


def bonus_materials(hero, player_castle):
    """Bonus-Zutaten eines Angriffs aus der Ausrüstung: [(TemplateId, Anzahl)]."""
    out = []
    for effect_id, (quality, count, player_only) in BONUS_MATERIALS.items():
        if player_only and not player_castle:
            continue
        if random.random() >= hero_effect(hero, effect_id):
            continue
        pool = (_materials().get(quality) if quality
                else [m for ms in _materials().values() for m in ms])
        if pool:
            out.append((random.choice(pool), count))
    return out


def corpse_materials(castle):
    """Handwerksmaterial aus einer Heldenleiche (HeroCorpseSettings):
    Anzahl nach den Bau-Punkten der Kreaturen der Burg, Qualität nach
    ConsumableQualityTable. [(TemplateId, Anzahl)]"""
    s = _load("GeneralSettings", "HeroCorpseSettings.json")
    total_cp = sum(loot.creature(c["SpecContainerId"])[0]
                   for room in castle.get("Rooms") or []
                   for c in room.get("Creatures") or [])
    rows = [r for r in s["ConsumableDropCountTable"] if total_cp >= r.get("MinCP", 0)]
    count = _weighted([(p["DropCount"], p["Probability"])
                       for p in rows[-1]["ItemDropProbabilities"]]) or 0
    out = {}
    for _ in range(count):
        # ConsumableTypeTable: Dye hat Gewicht 0, nur CraftingMaterial
        quality = _weighted(list(s["ConsumableQualityTable"].items()))
        pool = _materials().get(quality)
        if pool:
            tid = random.choice(pool)
            out[tid] = out.get(tid, 0) + 1
    return list(out.items())


def material_templates(quality):
    """Lootbare Zutaten-Templates einer Qualität."""
    return _materials().get(quality) or []


def mine_material(chances):
    """Template einer Zutat mit Qualität nach chances ({Qualität: Gewicht})."""
    return random.choice(material_templates(_weighted(list(chances.items()))))


def material_item(quality):
    """Eine Zutat der Qualität als HeroConsumableItem (so liegen Zutaten in
    Beute und Material-O-Matic, je Stück ein Eintrag)."""
    return {"$type": T("HeroConsumableItem"),
            "TemplateId": random.choice(material_templates(quality))}


@functools.cache
def _material_packs():
    return {p["Id"]: p for p in _load("GeneralSettings", "CraftingMaterialsPacksSettings.json")
            ["CraftingMaterialsPacks"]}


def material_pack(pack_id):
    """Inhalt eines Zutatenpakets (CraftingMaterialsPacksSettings):
    [(TemplateId, Anzahl)], gleiche Zutaten zusammengefasst. V1-Pakete geben
    feste Anzahlen je Qualität, V2-Pakete würfeln die Qualität je Stück."""
    p = _material_packs()[pack_id]
    qualities = [q for q, n in (p.get("RandomCraftingMaterialsByQuality") or {}).items()
                 for _ in range(n)]
    for group in p.get("RandomCraftingMaterialsByQualityList") or []:
        qualities += [_weighted(list(group["Qualities"].items()))
                      for _ in range(group["Count"])]
    out = {}
    for q in qualities:
        if _materials().get(q):
            tid = random.choice(_materials()[q])
            out[tid] = out.get(tid, 0) + 1
    for m in p.get("FixedCraftingMaterials") or []:
        out[m["TemplateId"]] = out.get(m["TemplateId"], 0) + m.get("Amount", 1)
    return list(out.items())