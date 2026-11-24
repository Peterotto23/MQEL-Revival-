"""Loot for PVE attacks from the client's GameplaySettings.

Backed by the data:
- Only creatures drop anything in PVE (traps: PVEDroppableLootTypeMask 0,
  AttackerRewardSettings.AllowDecorationLoot false).
- XP per creature = ConstructionPoints * XpLootBaseCpMultiplier(castle level)
  * XpLootPerLevelCpMultiplier * XpModifierTable(level difference).
- Health orb fragments = LootableSpec.HealthOrbFragmentsLootBase
  * HealthOrbFragmentsModifierTable(level difference).
- Treasure room = TreasureRoom{Min,Max}{Gold,LifeForce}Ratio of the total loot.
- Hero level from HeroSettings.XpPerLevel (cumulative thresholds).

Gold/life force per creature = ConstructionPoints * Total{Gold,LifeForce}Modifier
* (1 + LEVEL_STEP * (castle level - 1)). Calibrated against original captures
(MQELOffline_cpp, AttackService): tutorial castle 2 (level 1, 30.6 CP) yielded
KillsGold 33 = KillsLifeForce 33; castle 101 (level 3, 79 CP) KillsGold 133,
KillsLifeForce 119, treasure room 46 each. The formula itself is not known.
"""

import functools
import glob
import json
import os
import random

import balance

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(ROOT, "client", "GameData", "Data", "GameplaySettings")

LEVEL_STEP = 0.25

# Buildings/<Id>: storages (treasure room chests) and mines -> wallet field
STORAGES = {3: "InGameCoin", 4: "LifeForce"}          # GoldStorage, LifeForceStorage
MINES = {5: "InGameCoin", 6: "LifeForce", 16: "PremiumCash"}  # gold/life force/bling
MATERIAL_MINE = 17  # Material-O-Matic (CraftingMaterialMine)


def _load(*parts):
    with open(os.path.join(SETTINGS, *parts), encoding="utf-8") as f:
        return json.load(f)


@functools.cache
def rewards():
    return _load("GeneralSettings", "AttackerRewardSettings.json")


@functools.cache
def xp_per_level():
    return _load("GeneralSettings", "HeroSettings.json")["XpPerLevel"]


@functools.cache
def _creature_spec(spec_id):
    """ConstructionPoints, HealthOrbFragmentsLootBase, rank of a creature."""
    path = glob.glob(os.path.join(SETTINGS, "Creatures",
                                  f"{spec_id:06d} - *", "Gameplay.json"))
    spec = {"cp": 0, "orbs": 0, "rank": "Grunt"}
    if path:
        with open(path[0], encoding="utf-8") as f:
            for comp in json.load(f):
                t = comp["$type"].split(",")[0].rsplit(".", 1)[-1]
                if t == "DynamicCpZoneMemberSpec":
                    spec["cp"] = comp.get("ConstructionPoints", 0)
                elif t == "LootableSpec":
                    spec["orbs"] = comp.get("HealthOrbFragmentsLootBase", 0)
                elif t == "CreatureSpec":
                    spec["rank"] = comp.get("Rank", "Grunt")
    return spec


def creature(spec_id):
    """(ConstructionPoints, HealthOrbFragmentsLootBase) of a creature."""
    s = _creature_spec(spec_id)
    return s["cp"], s["orbs"]


def creature_rank(spec_id):
    """Grunt/Captain/Elite/Boss (CreatureSpec.Rank, default Grunt)."""
    return _creature_spec(spec_id)["rank"]


def level_modifier(table, hero_level, castle_level):
    """Value from a {Minus5Levels..Plus6Levels} table.

    "PlusN" = hero N levels above the castle (less yield), "MinusN" below.
    """
    diff = hero_level - castle_level
    if diff == 0:
        return table["SameLevel"]
    if diff > 0:
        n = min(diff, 6)
        return table["Plus1Level" if n == 1 else f"Plus{n}Levels"]
    n = min(-diff, 5)
    return table["Minus1Level" if n == 1 else f"Minus{n}Levels"]


def _base_cp_multiplier(level):
    for row in rewards()["XpLootBaseCpMultiplierTable"]:
        if row["MinLevel"] <= level <= row["MaxLevel"]:
            return row["XpLootBaseCpMultiplier"]
    return 0


def creature_loot(castle, hero_level, rare_ratio=0.0):
    """{entity id: {Gold, LifeForce, Xp, HealthOrbFragments}} of the castle.
    rare_ratio: Loot-O-Meter bonus on gold and life force
    (castles.rare_ratio)."""
    r = rewards()
    level = castle["Level"]
    xp_mod = level_modifier(r["XpModifierTable"], hero_level, level)
    orb_mod = level_modifier(r["HealthOrbFragmentsModifierTable"],
                             hero_level, level)
    xp_per_cp = (_base_cp_multiplier(level) * r["XpLootPerLevelCpMultiplier"]
                 * xp_mod)
    level_factor = (1 + LEVEL_STEP * (level - 1)) * (1 + rare_ratio)
    out = {}
    for room in castle.get("Rooms") or []:
        for c in room.get("Creatures") or []:
            cp, orbs = creature(c["SpecContainerId"])
            out[c["Id"]] = {
                "Gold": round(cp * level_factor * r["TotalGoldModifier"]),
                "LifeForce": round(cp * level_factor
                                   * r["TotalLifeForceModifier"]),
                "Xp": round(cp * xp_per_cp),
                "HealthOrbFragments": round(orbs * orb_mod),
            }
    return out


@functools.cache
def attack_settings():
    return _load("GeneralSettings", "AttackSettings.json")


def loss_ponderation(hero_level, castle_level, table="LossPonderationTable"):
    """Row of AttackSettings.LossPonderationTable or
    MineLossPonderationTable (loss share and shield of the defender), like
    AttackSettingsExtensions.GetLossPonderation/GetMineLossPonderation in the
    original server: difference = attacker's hero level - defender's castle
    level; the row with exactly this AttackerDefenderLevelDiff (0 if not
    given), below it the first row, above it the last."""
    rows = sorted(attack_settings()[table], key=lambda r: r.get("AttackerDefenderLevelDiff", 0))
    diff = hero_level - castle_level
    exact = next((r for r in rows if r.get("AttackerDefenderLevelDiff", 0) == diff), None)
    if exact:
        return exact
    return rows[0] if diff < rows[0].get("AttackerDefenderLevelDiff", 0) else rows[-1]


def mine_stealable(status, castle_level, hero_level):
    """(loot, protected) of a real player's mine, like
    MineManager.CalculateMineStealableAmount/-MaxStealableAmount in the
    original: mine shield MineShieldedAmoutDrop, empty (or rank 0)
    MineEmptyAmountDrop - both cost the owner nothing (protected) -,
    otherwise at most the share per MineLossPonderationTable of the production
    of MinePeriodStealableAmountInSeconds (default 1 h, _HourlyProduction from
    castles.player_castle), at most the fill level. Destroyed mines remain
    lootable."""
    s = attack_settings()
    if status.get("_Shielded"):
        return s["MineShieldedAmoutDrop"], True
    production = status.get("_HourlyProduction", 0)
    if not production or not status.get("CumulatedAmount"):
        return s["MineEmptyAmountDrop"], True
    share = loss_ponderation(hero_level, castle_level,
                             "MineLossPonderationTable").get("AttackCastlePercentageLoss", 0)
    # as in the original: Math.Floor(Math.Round(share * production, 1) + 0.5), at least 1
    maximum = max(1, int(round(share * production, 1) + 0.5))
    return min(maximum, status["CumulatedAmount"]), False


def treasure_room(total_gold, total_lifeforce, castle=None, hero_level=1):
    """(gold, life force) of the treasure room. Castle of a real player
    (_Wallet): a share of their wallet per loss_ponderation, which they lose
    when looted (LootManager.CalculateTreasureRoomStealableAmount in the
    original server); otherwise generated as a share of the creature loot."""
    wallet = (castle or {}).get("_Wallet")
    if castle is not None and wallet is not None:
        share = loss_ponderation(hero_level, castle["Level"]).get("AttackCastlePercentageLoss", 0)
        return int(wallet["InGameCoin"] * share), int(wallet["LifeForce"] * share)
    r = rewards()
    return (round(total_gold * r["TreasureRoomMaxGoldRatio"]),
            round(total_lifeforce * r["TreasureRoomMaxLifeForceRatio"]))


def level_for_xp(xp):
    """Hero level for cumulative XP (thresholds from HeroSettings)."""
    table = xp_per_level()
    level = 1
    for i, need in enumerate(table):
        if xp >= need:
            level = i + 1
    return level


def stealable_buildings(castle, total_gold, total_lifeforce, hero_level=1):
    """{building id: (wallet field, amount, is_storage, protected)} of the
    lootable buildings. Storages share the treasure room loot
    (treasure_room). Gold/life force mines of real players (_MineStatuses)
    per mine_stealable; bling mines give up their fill level, mines of
    Ubisoft castles UbisoftCastleMineAmountDrop. protected: the owner
    loses nothing (mine shield, empty mine)."""
    statuses = castle.get("_MineStatuses")
    tr_gold, tr_lf = treasure_room(total_gold, total_lifeforce, castle, hero_level)
    storage_amount = {"InGameCoin": tr_gold, "LifeForce": tr_lf}
    # Ubisoft castles: flat UbisoftCastleMineAmountDrop per gold/life force
    # mine (final AttackSettings only; the 2014 DLL knows no mine loot for
    # Ubisoft castles, the client does not read the value)
    drop = attack_settings()["UbisoftCastleMineAmountDrop"]
    mine_amount = {"InGameCoin": drop, "LifeForce": drop, "PremiumCash": 0}
    out = {}
    for room in castle.get("Rooms") or []:
        for b in room.get("Buildings") or []:
            spec = b["SpecContainerId"]
            if spec in STORAGES:
                field = STORAGES[spec]
                out[b["Id"]] = (field, storage_amount[field], True, False)
            elif spec in MINES:
                field = MINES[spec]
                if statuses is None:
                    out[b["Id"]] = (field, mine_amount[field], False, False)
                    continue
                s = statuses.get(str(b["Id"])) or {}
                if field == "PremiumCash":
                    amount = (0 if s.get("IsDestroyed") or not s.get("IsActive", True)
                              else s.get("CumulatedAmount", 0))
                    if balance.enabled():  # only a few hours of production
                        # StealHours of production (0 = not lootable); the
                        # rate is at least 1 per hour, so at least 1 bling
                        steal = balance.get("Mine", "StealHours") * balance.get("Mine", "BlingsPerHour")
                        amount = min(amount, steal)
                    out[b["Id"]] = (field, amount, False, False)
                else:
                    amount, protected = mine_stealable(s, castle["Level"], hero_level)
                    out[b["Id"]] = (field, amount, False, protected)
    return out


def stealable_materials(castle):
    """{building id: (ingredients, protected)} of the Material-O-Matics
    (AttackSettings.CraftingMaterialMine*ItemDropCount): a full pack gives up
    that many of its ingredients, which the owner loses; empty or with a mine
    shield, the same number of Basic ingredients, which cost the owner
    nothing. Castles without mine statuses (no player castle) count as
    empty."""
    import item_drops  # item_drops imports loot
    s = attack_settings()
    statuses = castle.get("_MineStatuses") or {}
    out = {}
    for room in castle.get("Rooms") or []:
        for b in room.get("Buildings") or []:
            if b["SpecContainerId"] != MATERIAL_MINE:
                continue
            st = statuses.get(str(b["Id"])) or {}
            pack = st.get("CumulatedHeroItems") or []
            if pack and not st.get("_Shielded"):
                n = min(s["CraftingMaterialMineItemDropCount"], len(pack))
                out[b["Id"]] = ([dict(i) for i in random.sample(pack, n)], False)
            else:
                out[b["Id"]] = ([item_drops.material_item("Basic") for _ in
                                 range(s["CraftingMaterialMineEmptyItemDropCount"])], True)
    return out


# --- Rare bling loot (balance "Loot") -----------------------------------------

BLING_LOOT = "BlingLootLog"  # account: [[Unix time, blings], ...]
BLING_LOOT_WINDOW = 7 * 86400


def bling_loot_left(account, now):
    """Blings that can still drop from loot within the last 7 days;
    None = unlimited (WeeklyCap 0)."""
    cap = balance.get("Loot", "WeeklyCap")
    if cap <= 0:
        return None
    recent = sum(n for t, n in account.get(BLING_LOOT) or []
                 if t > now - BLING_LOOT_WINDOW)
    return max(0, cap - recent)


def bling_drop_allowed(pvp):
    """Whether creatures may carry blings in this kind of castle: pvp True =
    player castle (Loot.PvP), False = campaign castle (Loot.PvE); None (kind
    unknown) = allowed."""
    return pvp is None or bool(balance.get("Loot", "PvP" if pvp else "PvE"))


def add_bling_drop(creature_loot, account, now, rng=random, pvp=None):
    """With Loot.Chance, puts the blings on a random creature
    (DefenseIngredientLoot.PremiumCash); capped to the remaining budget.
    pvp: True for a player castle, False for a campaign castle (the attack
    passes it; see bling_drop_allowed)."""
    if not balance.enabled() or not creature_loot or not bling_drop_allowed(pvp):
        return
    if rng.random() * 100 >= balance.get("Loot", "Chance"):
        return
    amount = balance.get("Loot", "Amount")
    left = bling_loot_left(account, now)
    if left is not None:
        amount = min(amount, left)
    if amount > 0:
        creature_loot[rng.choice(sorted(creature_loot))]["PremiumCash"] = amount


def record_bling_loot(account, amount, now):
    """Enter loot blings into the rolling window (old entries drop out)."""
    log = [e for e in account.get(BLING_LOOT) or [] if e[0] > now - BLING_LOOT_WINDOW]
    if amount:
        log.append([int(now), amount])
    account[BLING_LOOT] = log

# --- Boss raids (balance "Boss") ----------------------------------------------

BOSS_LOG = "BossBlingLog"  # account: [[Unix time, blings], ...]


def boss_bounty_left(account, now):
    """Blings that boss raids can still pay within the last 7 days;
    None = unlimited (WeeklyCap 0)."""
    cap = balance.get("Boss", "WeeklyCap")
    if cap <= 0:
        return None
    recent = sum(n for t, n in account.get(BOSS_LOG) or []
                 if t > now - BLING_LOOT_WINDOW)
    return max(0, cap - recent)


def boss_bounty(account, castle_id, now):
    """Blings for a won raid on the boss dungeon castle_id: the boss's amount
    (balance Boss.<castle id>), capped to the remaining weekly budget; 0 when
    the economy is off or the castle is no boss."""
    if not balance.enabled() or castle_id not in balance.BOSS_CASTLES:
        return 0
    amount = balance.get("Boss", str(castle_id))
    left = boss_bounty_left(account, now)
    return amount if left is None else min(amount, left)


def record_boss_bounty(account, amount, now):
    """Enter paid boss blings into the rolling window (old entries drop out)."""
    log = [e for e in account.get(BOSS_LOG) or [] if e[0] > now - BLING_LOOT_WINDOW]
    if amount:
        log.append([int(now), amount])
    account[BOSS_LOG] = log