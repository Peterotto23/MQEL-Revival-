"""Angriffs-Lifecycle (AttackService) für PVE-Burgen.

StartAttack liefert AttackInfo in der Form des Update3-Clients
(Feldtypen per tools/client_members.py -t AttackInfo). Burg = Castle-Datei
aus den GameplaySettings, Held = gewählter Held des Accounts. Beute
je Kreatur würfelt loot.py beim Start; EndAttack rechnet ab, was der Client
als eingesammelt meldet.
"""

import json
import os
import random
import time
from typing import Any

import castles
import item_drops
import loot
import trophies
from account_state import INVENTORY_SLOTS_PER_TAB

T = castles.T



# Gezielter Angriff: aus einem Profil (Freunde, News, Chat, Willkommensseite,
# Clanliste) mit AttackType None auf eine Spielerburg; Profil-Knopf in
# friends/headerModule.js. AttackSource laut Client (0x68b0e6):
# RegularSelection 0, MachineLearning 1, Friends 2, News 3, Assignment 4,
# Revenge 5, Chat 6, SeasonalCompetition 7, WelcomePage 8, GuildMembersList 9
TARGETED_SOURCES = (2, 3, 6, 8, 9)


def is_targeted(attack_type, attack_source, c):
    return (attack_type == 0 and attack_source in TARGETED_SOURCES
            and castles.castle_type(c) == castles.CASTLE_TYPE_USER)


ATTACK_TYPE_VISIT = 2  # AttackType im Client: Validation 1, Visit 2, Revenge 3
ATTACK_TYPE_REVENGE = 3
ATTACK_SOURCE_REVENGE = 5  # AttackSource im Client


class AttackSession:
    """Angriffszustand eines Spielers zwischen StartAttack und EndAttack.

    EndAttack trägt die AttackId nur im binären Replay-Anhang, daher merkt
    sich der Server die von StartAttack (current). Die Bewertungsprämie
    wird bei EndAttack gewürfelt (der Client zeigt sie aus
    EndAttackInfo.CastleRatingFreePrize an) und bei RateCastle
    gutgeschrieben (prize)."""

    def __init__(self):
        self.current: dict[str, Any] = {}
        self.prize: Any = None
        self.rated_castle: Any = None  # Burg des zuletzt beendeten Angriffs (RateCastle)


RESURRECTION = os.path.join(castles.ROOT, "client", "GameData", "Data",
                            "GameplaySettings", "GeneralSettings",
                            "ResurrectionSettings.json")



def shield_duration(hero_level, castle_level):
    """Schilddauer in Sekunden für den Verteidiger, dessen Burg geplündert
    wurde (loot.loss_ponderation: Heldenlevel des Angreifers gegen Burglevel,
    wie LossManager.OnEndAttackContribution); 0: kein Schild (ab 6 Level
    Abstand)."""
    row = loot.loss_ponderation(hero_level, castle_level)
    return castles.duration(row["ShieldDuration"]) if row.get("ShieldDuration") else 0


# CurrencyType-Namen der GameplaySettings -> Enum-Wert im Client
CURRENCY_TYPE = {"PremiumCash": 1, "IGC": 2, "LifeForce": 4}


def _roll_rating_prize():
    table = loot.rewards()["CastleRatingFreePrizeTable"]
    prize = random.choices(table, weights=[p["Chance"] for p in table])[0]
    return _currency(prize["Amount"], CURRENCY_TYPE[prize["CurrencyType"]])


def rate_castle(session):
    """(Prämie, Burg-Id) des zuletzt beendeten Angriffs; danach verbraucht
    (0 Gold, keine Burg)."""
    prize, session.prize = session.prize, None
    castle, session.rated_castle = session.rated_castle, None
    return prize or _currency(0, CURRENCY_TYPE["IGC"]), castle


def resurrection_cost(hero_level, count):
    """Gold-Kosten der (count+1)-ten Wiederbelebung, Staffel nach
    Helden-Level (ResurrectionManager.ComputeResurrectionCost)."""
    with open(RESURRECTION, encoding="utf-8") as f:
        rs = json.load(f)
    base = next((c.get("Cost", 0) for c in rs["CostsByLevel"]
                 if hero_level <= c["LevelTo"]),
                rs["CostsByLevel"][-1].get("Cost", 0))
    malus = rs["MalusByResurrectionCount"]
    return int(base * malus[min(count, len(malus) - 1)])


def _tiers(c, kind):
    """CreatureTiers/TrapTiers wie CastleAssembler.ComputeCreatureTiersFrom-
    CastleCreatures: ein Eintrag je Art, die in der Burg steht, Stufe und
    Spezialisierung aus der Burg, sonst 1/0. Den Burgdateien fehlen für die
    meisten Arten Einträge; ohne ihn bekamen diese Kreaturen trotz
    ForceCastleLevelOnBuildables nur ihr Stufe-1-Level (Level 1-9 in einer
    Level-18-Burg, nur einzelne auf Burg-Level)."""
    given = {t["SpecContainerId"]: t for t in c.get(kind + "Tiers") or []}
    specs = dict.fromkeys(e["SpecContainerId"] for r in c.get("Rooms") or []
                          for e in r.get(kind + "s") or [])
    return [{"$type": T("DefenseIngredientTier"), "SpecContainerId": s,
             "Tier": given.get(s, {}).get("Tier", 1),
             "Specialization": given.get(s, {}).get("Specialization", 0)}
            for s in specs]


def _castle(c):
    """Burg als UbisoftCastle (Klasse der Castle-Dateien selbst).

    Nur damit liest der Client Level, ForceCastleLevelOnBuildables usw.;
    als bloßes Castle fehlt ihm das Burg-Level (Kreaturen mit AutoLevelUp
    bekamen negative Level).
    """
    out: dict[str, Any] = {"$type": T("UbisoftCastle")}
    out.update((k, v) for k, v in c.items() if k != "$type" and not k.startswith("_"))
    for kind in ("Creature", "Trap"):
        out[kind + "Tiers"] = _tiers(c, kind)
    out.setdefault("CreationDate", "")
    out.setdefault("ModificationDate", "")
    return out


def inventory_capacity(account):
    return account["Inventory"]["InventoryTabCount"] * INVENTORY_SLOTS_PER_TAB


def free_inventory_slots(account):
    return inventory_capacity(account) - len(account["Inventory"]["HeroItems"])


def max_potion_use(hero_level):
    """Tränke je Angriff (AttackSettings.PotionUsageRestrictions), None =
    unbegrenzt."""
    for row in loot.attack_settings().get("PotionUsageRestrictions") or []:
        if row.get("HeroLevelMin", 0) <= hero_level <= row.get("HeroLevelMax", 10 ** 6):
            return row.get("MaxPotionUse")
    return None


def attack_belt(hero):
    """Gürtel für den Angriff: Vorrat je Slot auf zusammen höchstens
    max_potion_use gekürzt (in Slot-Reihenfolge). Im Lager ist der Gürtel
    gleich dem Inventarvorrat; ungekürzt trank der Held alles leer."""
    left = max_potion_use(hero["Level"])
    belt = []
    for b in sorted(hero.get("EquippedConsumables") or [], key=lambda x: x["SlotIndex"]):
        n = b["StackCount"] if left is None else min(b["StackCount"], left)
        if left is not None:
            left -= n
        belt.append(dict(b, StackCount=n))
    return belt


def inventory_consumables(belt):
    """Vorrat an Verbrauchsgütern für den Angriff ({TemplateId, StackCount}
    je Template, Form laut MQELOffline_cpp AttackService::StartAttack). Daran
    misst der Client im Angriff, wie viele Tränke er trinken darf ("nicht
    genug Tränke"), daher nur der gekürzte Gürtel (attack_belt)."""
    counts = {}
    for b in belt:
        counts[b["TemplateId"]] = counts.get(b["TemplateId"], 0) + b["StackCount"]
    return [{"TemplateId": tid,  # Typname unbekannt, Mitschnitt ohne $type
             "StackCount": n} for tid, n in sorted(counts.items()) if n]


def _currency(amount, currency_type):
    return {"$type": T("CurrencyAmount"), "Amount": amount,
            "CurrencyType": currency_type}


def _custom_trap_items(custom, hero_spec):
    """{Fallen-Id: [Items]} aus Castle.CustomAttackerReward.CustomLoots
    (Tutorial 1: Falle 1 gibt die Startwaffe je Held; Original-Mitschnitt
    AttackService.cpp:52 sendet sie so als TrapLoot)."""
    out = {}
    for cl in custom.get("CustomLoots") or []:
        if cl.get("Type") != "Trap":
            continue
        for it in (cl.get("PerHeroItem") or {}).get(str(hero_spec), []):
            item = {"$type": T("HeroEquipmentItem"), "ArchetypeId": 0,
                    "DyeTemplateId": 0, "Effects": [], "IsBranded": False,
                    "IsSellable": True, "ItemLevel": 1,
                    "PrimaryStatsModifiers": [1, 1, 1]}
            item.update({k: v for k, v in it.items() if k != "$type"})
            out.setdefault(cl["BuildableId"], []).append(item)
    return out


def own_castle(account):
    """Eigene Burg (Entwurf) in Form einer Burgdatei, für die Validierung."""
    return castles.player_castle(account, draft=True)


def _revenge_id(account, body, practice):
    """AttackId des Verteidigungslog-Eintrags, für den gerade Rache
    genommen wird. Der Client lässt revengeAttackId leer (Weg über das
    Burg-Popup), dann der neueste offene Eintrag dieses Angreifers."""
    if practice or (body.get("attackSource") != ATTACK_SOURCE_REVENGE
                    and body.get("attackType") != ATTACK_TYPE_REVENGE):
        return None
    if body.get("revengeAttackId"):
        return body["revengeAttackId"]
    for entry in (account.get("DefendLog") or {}).get("DefendLogEntries") or []:
        if (entry.get("RevengeEnabled") and (entry.get("AttackerAccountSummary") or {})
                .get("Id") == body.get("castleAccountId")):
            return entry.get("AttackId")
    return None


def start_attack(account, body, session, boosts=([], {}), delegated=None):
    """AttackInfo für StartAttack; None, wenn die Burg unbekannt ist.
    boosts = (aktive Boosts, Zusatzfaktoren) aus AccountState.attack_boosts;
    sie gelten für den ganzen Angriff. delegated: fremde Burg im Stand ihres
    Validierungs-Tickets (castles.validation_castle), wenn der Client mit
    AttackType Validation angreift."""
    validation = body.get("castleAccountId") == account["AccountId"]
    delegated = (delegated if not validation
                 and body.get("attackType") == castles.ATTACK_TYPE_VALIDATION else None)
    # Validierung (Burg testen, auch für andere) und Test-Angriff auf Burgen
    # von Freunden und Clan-Mitgliedern (AttackType Visit) bringen keine
    # Beute und Kronen
    practice = (validation or bool(delegated)
                or body.get("attackType") == ATTACK_TYPE_VISIT)
    c = (own_castle(account) if validation
         else delegated or castles.find_castle(body.get("castleAccountId")))
    if c is None:
        return None
    hero = next((h for h in account["Heroes"]
                 if h["HeroSpecContainerId"] == account["SelectedHeroId"]),
                account["Heroes"][0])
    # $type erzeugt nur eine harmlose "Unhandled member"-Warnung
    settings = dict(body.get("userSettings") or {})
    settings["$type"] = "Contracts.Common.Settings.AttackUserSettings, Contracts.Common"
    # Validierung (Burg testen, AttackType 1): eigene Burg, keine Beute
    # Loot-O-Meter: Bonus auf Gold/Lebenskraft und (Spieltext "increased
    # chance of getting rare loot") auf die Qualität der Kreatur-Drops
    rare_ratio = castles.rare_ratio(c)
    creature_loot = {} if practice else loot.creature_loot(
        c, hero["Level"], rare_ratio)
    if not (c.get("IsTutorialCastle") or body.get("ubisoftCompetitionId")):
        loot.add_bling_drop(creature_loot, account, time.time())
    tr_gold, tr_lf = loot.treasure_room(
        sum(x["Gold"] for x in creature_loot.values()),
        sum(x["LifeForce"] for x in creature_loot.values()), c, hero["Level"])
    # Validierung: Minen mit Betrag 0, sonst Assert "no stealable mines"
    buildings = loot.stealable_buildings(
        c, sum(x["Gold"] for x in creature_loot.values()),
        sum(x["LifeForce"] for x in creature_loot.values()), hero["Level"])
    # Burgschild: die Schatzkammer (mit den Lagern) bietet nichts; Minen
    # schützt nur ihr eigener Minenschild (Original: LootManager.
    # CalculateTreasureRoomStealableLoot prüft den Schild, MineManager nicht)
    shielded = not practice and bool(c.get("_ShieldDue"))
    if shielded or practice:  # Übung: auch kein Anteil am Wallet des Besitzers
        tr_gold, tr_lf = 0, 0
        buildings = {bid: (b[0], 0, b[2], True) if b[2] or practice else b
                     for bid, b in buildings.items()}
    if validation or delegated:
        # Lager stehen als Truhen in der Schatzkammer; der Client zählte sie
        # sonst zu den Minen, die vor der Schatzkammer zerstört sein müssen
        buildings = {bid: b for bid, b in buildings.items() if not b[2]}
    # Material-O-Matics; beim Testen der eigenen Burg ohne Zutaten, aber
    # aufgeführt: der Client zählt sie zu den Minen (Assert "number of
    # destroyed mines is greater than the number of stealable mines")
    materials = loot.stealable_materials(c)
    if practice or validation or delegated:
        materials = {bid: ([], True) for bid in materials}
    custom = c.get("CustomAttackerReward") or {}
    if practice or custom.get("DisableItemDrop") or custom.get("CustomLoots"):
        items, tr_item = {}, None  # Tutorial-Burgen: nur die feste Beute
    else:
        # Magic Find: Boost plus Effekt der angelegten Ausrüstung plus
        # Loot-O-Meter
        magic_find = (boosts[1].get("MagicFind", 0)
                      + item_drops.hero_effect(hero, item_drops.MAGIC_FIND)
                      + rare_ratio)
        items = item_drops.creature_items(c, hero["HeroSpecContainerId"], magic_find,
                                           hero["Level"])
        # Spielerburgen: zusätzlich Handwerksmaterial/Färbemittel
        if castles.castle_type(c) == castles.CASTLE_TYPE_USER:
            for eid, drops in item_drops.creature_consumables(
                    c, account.get("BuildInfo", {}).get("Level", 1)).items():
                items.setdefault(eid, []).extend(drops)
        event = castles.active_event(c)
        tr_item = (item_drops.event_treasure_item(event, c["Level"],
                                                  hero["HeroSpecContainerId"])
                   if event else None) or item_drops.treasure_item(
                       c, hero["HeroSpecContainerId"], hero["Level"])
    belt = attack_belt(hero)
    trap_items = _custom_trap_items(custom, hero["HeroSpecContainerId"])
    session.current = {
        "castle": c,
        "hero": hero["HeroSpecContainerId"],
        "hero_level": hero["Level"],
        "resurrections": 0,
        "attack_id": f"{random.getrandbits(96):024x}",
        "started": time.monotonic(),  # Zeitprüfung (anticheat)
        "loot": creature_loot,
        "treasure": (tr_gold, tr_lf),
        "items": items,
        "treasure_item": tr_item,
        "buildings": buildings,
        "material_mines": materials,
        "trap_items": trap_items,
        "validation": validation,
        "practice": practice,
        "delegated": bool(delegated),
        "completion_reward": castles.validation_reward(delegated),
        "attack_type": body.get("attackType", 0),
        "competition": body.get("ubisoftCompetitionId", 0),
        "revenge": _revenge_id(account, body, practice),
        "targeted": is_targeted(body.get("attackType", 0),
                                body.get("attackSource", 0), c),
        "boosts": boosts[1],
        "shielded": shielded,
        "par": 0.0 if validation else castles.par_time(c),
        "trophies": ((0, 0, 0) if practice else
                     trophies.variation(account, c, hero["HeroSpecContainerId"])),
    }
    return {
        "$type": T("AttackInfo"),
        # Spielerburgen: Held wird aufs Burg-Level herabgestuft
        # ("Your Hero will be adjusted to the castle level")
        "AdjustedHeroLevel": (min(hero["Level"], c["Level"])
                              if castles.castle_type(c) == castles.CASTLE_TYPE_USER
                              else hero["Level"]),
        "AttackerActiveConsumables": boosts[0],
        "AttackerDisplayName": account["DisplayName"],
        # MongoDB-ObjectId als 24 Hex-Zeichen (Client-Puffer: 24 + NUL)
        "AttackId": session.current["attack_id"],
        "AttackRandomSeed": random.randrange(1, 2**31),
        "AttackType": body.get("attackType", 0),
        "AttackUserSettings": settings,
        "Castle": _castle(c),
        "CastleHeartRank": castles.heart_rank(c),
        "CastleType": castles.castle_type(c),
        "CastleValidationDuration": 0.0 if validation else castles.par_time(c),
        "CreatureLoot": [
            {"$type": T("DefenseIngredientLoot"), "Id": eid,
             "InventoryItems": items.get(eid, []), "PremiumCash": 0, **l}
            for eid, l in creature_loot.items()],
        "DecorationLoot": [],
        "DefenderActiveConsumables": [],
        "DefenderSpecialPacks": [],
        "FirstResurrectionCost": resurrection_cost(hero["Level"], 0),
        "FreeInventorySlotsCount": free_inventory_slots(account),
        "Hero": dict(hero, EquippedConsumables=belt),
        "InventoryConsumablesInfo": inventory_consumables(belt),
        "IsResurrectionAllowed": True,
        "IsRevenge": bool(session.current["revenge"]),
        "IsShielded": shielded,
        "IsTargetedAttack": is_targeted(body.get("attackType", 0),
                                        body.get("attackSource", 0), c),
        "IsTutorial": bool(c.get("IsTutorialCastle")),
        "Level": c["Level"],
        "LoseTrophyCooldownTimer": trophies.lose_cooldown(
            account, c.get("AccountId"), hero["HeroSpecContainerId"]) or trophies.NEVER,
        # Ohne Eintrag zeigt der Client Truhen/Minen im Käfig (nicht plünderbar)
        "StealableMines": [
            {"$type": T("StealableMine"), "CastleBuildingId": bid,
             "IsShielded": shielded if storage else bool(_mine_shielded(c, bid)),
             "MaxStealableAmount": amount,
             "StealableAmount": amount, "StealableHeroItems": []}
            for bid, (_, amount, storage, _) in buildings.items()] + [
            {"$type": T("StealableMine"), "CastleBuildingId": bid,
             "IsShielded": bool(_mine_shielded(c, bid)),
             "MaxStealableAmount": len(its), "StealableAmount": len(its),
             "StealableHeroItems": its}
            for bid, (its, _) in materials.items()],
        "TargetedAttackAvailableCount": account.get("TargetedAttackAvailableCount", 0),
        "TrapLoot": [
            {"$type": T("DefenseIngredientLoot"), "Id": tid, "InventoryItems": its,
             "Gold": 0, "LifeForce": 0, "Xp": 0, "HealthOrbFragments": 0,
             "PremiumCash": 0}
            for tid, its in trap_items.items()],
        "TreasureRoomGoldRatio": loot.rewards()["TreasureRoomMaxGoldRatio"],
        "TreasureRoomHeroItem": tr_item,
        "TreasureRoomLifeForceRatio": loot.rewards()["TreasureRoomMaxLifeForceRatio"],
        "TreasureRoomReward": None,
        "TreasureRoomStealableIGC": _currency(tr_gold, 2),
        "TreasureRoomStealableLifeForce": _currency(tr_lf, 4),
        "TrophyScoreDefender": trophies.castle_score(c),
        "TrophyScoreGain": session.current["trophies"][0],
        "TrophyScoreLost": session.current["trophies"][1],
        "TrophyScoreLostWithoutCooldown": session.current["trophies"][2],
        "TrophyScoreRestartAttackGain": session.current["trophies"][0],
        "TrophyScoreRestartAttackLost": session.current["trophies"][1],
        "TrophyScoreRestartLostWithoutCooldown": session.current["trophies"][2],
        "UbisoftCompetitionBestTime": 0,
        "UbisoftCompetitionId": body.get("ubisoftCompetitionId", 0),
        "UnlockedEmotes": account.get("UnlockedEmotes", []),
        "UnlockedSpells": account.get("UnlockedSpells", []),
        "VictoryConditionRewardRatios": [],
    }


def _sum(cur, field, ids):
    table = cur.get("loot") or {}
    return sum(table[i].get(field, 0) for i in set(ids or []) if i in table)


def _mine_shielded(c, bid):
    return ((c.get("_MineStatuses") or {}).get(str(bid)) or {}).get("_Shielded")


def _pillaged_mines(cur, pillaged):
    """Beute aus Minen nach PillagedMine {CastleBuildingId, StolenAmount,
    IsDestroyed}, je Mine auf das Angebotene gedeckelt: ({Wallet-Feld:
    Summe}, {Building-Id: Verlust des Besitzers}, {zerstörte Building-Ids}).
    Geschützte Minen (Minenschild, leer) bringen dem Angreifer etwas, dem
    Besitzer kosten sie nichts. Lager laufen über die TreasureRoomLooted*-
    Felder und zählen hier nicht."""
    buildings = cur.get("buildings") or {}
    out = {"InGameCoin": 0, "LifeForce": 0, "PremiumCash": 0}
    per_mine, destroyed, seen = {}, set(), set()
    for p in pillaged or []:
        bid = p.get("CastleBuildingId")
        b = buildings.get(bid)
        if b is None or b[2] or bid in seen:
            continue
        seen.add(bid)
        amount = max(0, min(p.get("StolenAmount", 0), b[1]))
        out[b[0]] += amount
        per_mine[bid] = 0 if b[3] else amount
        if p.get("IsDestroyed"):
            destroyed.add(bid)
    return out, per_mine, destroyed


def _pillaged_materials(cur, pillaged):
    """Geplünderte Material-O-Matics (PillagedMine mit deren Building-Id):
    ([Zutaten für den Angreifer], {Building-Id: Zutaten, die der Besitzer
    verliert}, {zerstörte Building-Ids})."""
    offered = cur.get("material_mines") or {}
    got, lost, destroyed, seen = [], {}, set(), set()
    for p in pillaged or []:
        bid = p.get("CastleBuildingId")
        if bid not in offered or bid in seen:
            continue
        seen.add(bid)
        its, protected = offered[bid]
        got += its
        lost[bid] = [] if protected else its
        if p.get("IsDestroyed"):
            destroyed.add(bid)
    return got, lost, destroyed


def _mine_loss(cur, per_mine):
    """{Wallet-Feld: Summe} der Minenverluste des Besitzers."""
    buildings = cur.get("buildings") or {}
    out = {"InGameCoin": 0, "LifeForce": 0, "PremiumCash": 0}
    for bid, n in per_mine.items():
        out[buildings[bid][0]] += n
    return out


# VictoryConditionType aus Sicht des Verteidigers (Contracts)
DEFENSE_LOST, DEFENSE_WON = 1, 3


def _defense(cur, params, per_mine, destroyed, treasure, trophy_defender,
             materials=({}, set())):
    """Verteidigungslog-Eintrag (DefendLogEntry, Felder laut Client-Leser,
    ohne AttackerAccountSummary), Verluste der Minen ({Building-Id: Betrag}),
    zerstörte Minen und Schatzkammer-Beute ({Wallet-Feld: Betrag}, zieht
    record_defense vom Wallet ab) für den Besitzer einer Spielerburg."""
    won = params.get("CompletionType") == 0
    lost = _mine_loss(cur, per_mine)
    shielded = cur.get("shielded", False)
    castle_level = (cur.get("castle") or {}).get("Level", 1)
    mat_lost, mat_destroyed = materials
    return {"mines": per_mine, "destroyed": destroyed, "treasure": treasure,
            "material_mines": mat_lost, "material_destroyed": mat_destroyed,
            # Minenschild für zerstörte Minen (MineLossPonderationTable)
            "mine_shield": castles.duration(loot.loss_ponderation(
                cur.get("hero_level", 1), castle_level, "MineLossPonderationTable")
                .get("ShieldDuration") or "0s"),
            # geplündert: neuer Schild (eine geschützte Burg behält ihren)
            "shield": (shield_duration(cur.get("hero_level", 1), castle_level)
                       if won and not shielded else 0), "deaths": len(params.get("DeathDetails") or []),
            # Todesorte des Angreifers: je einer eine Heldenleiche
            "death_spots": [(d.get("GlobalX", 0), d.get("GlobalZ", 0))
                            for d in params.get("DeathDetails") or []],
            "won": won, "trophies": trophy_defender, "entry": {
        "$type": T("DefendLogEntry"),
        "AttackDurationInMilliseconds": int(params.get("AttackFrameCount", 0)
                                            * params.get("AttackFrameDuration", 0) * 1000),
        "AttackId": cur.get("attack_id", ""),
        "AttackType": cur.get("attack_type", 0),
        "CastleRating": params.get("CastleRating", 0),
        "CompletionType": params.get("CompletionType", 0),
        "HasReplay": False,
        "HeroSpecContainerId": cur.get("hero", 0),
        "IsCastleAttackable": True,
        "IsRevengeAttack": bool(cur.get("revenge")),
        "IsShielded": shielded,
        "IsTargetedAttack": cur.get("targeted", False),
        "Level": cur.get("hero_level", 1),
        "Message": None,
        "PillagedMines": [
            {"$type": T("PillagedMine"), "CastleBuildingId": bid,
             "IsDestroyed": bid in destroyed, "StolenAmount": n, "StolenHeroItems": []}
            for bid, n in per_mine.items() if n or bid in destroyed] + [
            {"$type": T("PillagedMine"), "CastleBuildingId": bid,
             "IsDestroyed": bid in mat_destroyed, "StolenAmount": len(its),
             "StolenHeroItems": its}
            for bid, its in mat_lost.items() if its or bid in mat_destroyed],
        "PotionUsed": len(_potions_used(params, cur.get("hero_level", 1))),
        "ResurrectionCount": cur.get("resurrections", 0),
        # Angreifer ist immer ein echter Spieler; ein Versuch je Eintrag
        # (BattleLogSettings.MaximumRevengeAttemptCount fehlt in den Settings)
        "RevengeEnabled": True,
        "StolenIGC": _currency(lost["InGameCoin"] + treasure["InGameCoin"], 2),
        "StolenLifeForce": _currency(lost["LifeForce"] + treasure["LifeForce"], 4),
        "TrophyScoreVariation": trophy_defender,
        "VictoryConditionLevel": stars(params, cur),
        "VictoryConditionRewardRatios": [],
        "VictoryConditionType": DEFENSE_LOST if won else DEFENSE_WON}}


def _looted_items(cur, looted, source="items"):
    """Items zu LootedHeroItemParam-Einträgen {ItemId: Kreatur-/Fallen-
    Entity, ItemIndex: Index in deren InventoryItems} (Feldnamen laut
    Client-Leser; die Contracts nennen ItemId "CreatureId"). Jedes Item
    höchstens einmal."""
    table = cur.get(source) or {}
    seen, out = set(), []
    for p in looted or []:
        key = (p.get("ItemId", p.get("CreatureId")), p.get("ItemIndex", 0))
        items = table.get(key[0], [])
        if key not in seen and 0 <= key[1] < len(items):
            seen.add(key)
            out.append(items[key[1]])
    return out


def _destroyed(castle, params):
    """{"Creature"|"Decoration"|"Building": {SpecContainerId: Anzahl}} der
    getöteten Kreaturen, zerstörten Dekorationen und zerstörten Minen
    (PillagedMines mit IsDestroyed; z. B. Objective 304 "Destroy the Gold
    Mines") für Objective-Bedingungen."""
    ids = {"Creature": set(params.get("KilledCreatureIds") or []),
           "Decoration": set(params.get("DestroyedDecorationIds") or []),
           "Building": {m.get("CastleBuildingId")
                        for m in params.get("PillagedMines") or []
                        if m.get("IsDestroyed")}}
    out = {"Creature": {}, "Decoration": {}, "Building": {}}
    for room in castle.get("Rooms") or []:
        for kind, key in (("Creature", "Creatures"), ("Decoration", "Decorations"),
                          ("Building", "Buildings")):
            for e in room.get(key) or []:
                if e["Id"] in ids[kind]:
                    spec = e["SpecContainerId"]
                    out[kind][spec] = out[kind].get(spec, 0) + 1
    return out


# Sterne (VictoryConditionLevel) wie TrophyManager.CalculateVictoryConditionLevel
# im Original-Server, unabhängig voneinander: einer für die Schatzkammer, einer
# für eine Zeit unter der Validierungszeit (Par; im finalen Client abzüglich
# AttackSettings.MineDestroyedTimeBonus je zerstörter Mine, wie sein Timer),
# einer, wenn der Anteil zerstörter Minen AttackerRewardSettings.
# MineDestroyedVictoryCondition erreicht (1: alle). Eine Wiederbelebung
# (HeroDefeatedCount) heißt 0 Sterne.
MINE_TIME_BONUS = 3


def _potions_used(params, hero_level):
    """Gemeldete Tränke, höchstens max_potion_use (manipulierter Client)."""
    used = params.get("ConsumableUsed") or []
    return used[:max_potion_use(hero_level)]


def stars(params, cur):
    par = cur.get("par")
    if (params.get("CompletionType") != 0 or not params.get("TreasureRoomOpenFrameCount")
            or cur.get("resurrections")):
        return 0
    if not par:  # Validierung: keine Sterne (MaxVictoryConditionLevel 0)
        return 0
    t = (params["TreasureRoomOpenFrameCount"] * params.get("AttackFrameDuration", 0)
         - MINE_TIME_BONUS * params.get("MineDestroyedTimeBonusCount", 0))
    mines = [bid for bid, b in (cur.get("buildings") or {}).items() if not b[2]]
    destroyed = {m.get("CastleBuildingId") for m in params.get("PillagedMines") or []
                 if m.get("IsDestroyed")} & set(mines)
    mine_star = bool(mines) and (len(destroyed) / len(mines)
                                 >= loot.rewards()["MineDestroyedVictoryCondition"])
    return 1 + (t < par) + mine_star


def boss_raid_blings(account, cur, params, suspicious=False):
    """Repeatable boss payout (balance "Boss"): blings for a won raid
    (CompletionType TreasureRoom, like objectives and complete_boss_region)
    on a region's boss dungeon, on every clear. Not for practice, tests of
    own/delegated castles, tutorial, competitions, a fight the anticheat
    flagged, or player castles that happen to share a boss's id."""
    c = cur.get("castle") or {}
    if (suspicious or cur.get("practice") or cur.get("competition")
            or c.get("IsTutorialCastle")
            or castles.castle_type(c) != castles.CASTLE_TYPE_UBISOFT
            or params.get("CompletionType") != 0):
        return 0
    return loot.boss_bounty(account, c.get("AccountId"), time.time())


def end_attack(account, params, session, suspicious=False):
    """(EndAttackInfo, Gutschrift) für EndAttack.

    Gutschrift nur für das, was der Client als eingesammelt meldet
    (Looted*CreatureIds, KilledCreatureIds für XP, Schatzkammer-Beträge,
    gedeckelt auf das beim Start Angebotene). Ein EndAttack ohne
    laufenden Angriff bringt nichts. suspicious: der Kampf fiel der
    Cheat-Erkennung auf (keine Boss-Prämie).
    """
    cur = session.current
    c = cur.get("castle") or {}
    wallet = account["Wallet"]
    hero = next((h for h in account["Heroes"]
                 if h["HeroSpecContainerId"] == cur.get("hero")), None)
    kills_gold = _sum(cur, "Gold", params.get("LootedGoldCreatureIds"))
    kills_lf = _sum(cur, "LifeForce", params.get("LootedLifeForceCreatureIds"))
    kills_xp = _sum(cur, "Xp", params.get("KilledCreatureIds"))
    # Bling-Beute: der Client meldet keine eigene Id-Liste, Blings fallen
    # wie XP beim Töten an
    kills_blings = _sum(cur, "PremiumCash", params.get("KilledCreatureIds"))
    # Boss-Prämie: je gewonnenem Raid auf die Bossburg einer Region
    boss_blings = boss_raid_blings(account, cur, params, suspicious)
    if c.get("IsTutorialCastle"):
        # Tutorial: XP aller Kreaturen, auch bei Niederlage oder Abbruch (das
        # Tutorial geht trotzdem weiter). Danach soll der Held Level 2 haben
        # (Zauber in die Leiste ziehen); mit beiden Tutorial-Burgen komplett
        # sind es 76 XP bei 75 nötigen, eine ausgelassene Kreatur blockierte
        # das Tutorial
        kills_xp = sum(x["Xp"] for x in (cur.get("loot") or {}).values())
    # nach einer Wiederbelebung ist die Schatzkammer verschlossen (der Client
    # zeigt die Truhen im Käfig; Original: EnterTreasureRoom nur ohne
    # HeroDefeatedCount)
    tr_gold_max, tr_lf_max = (0, 0) if cur.get("resurrections") else cur.get("treasure", (0, 0))
    tr_gold = min(params.get("TreasureRoomLootedGold", 0), tr_gold_max)
    tr_lf = min(params.get("TreasureRoomLootedLifeForce", 0), tr_lf_max)
    looted = (_looted_items(cur, params.get("LootedHeroItemCreatureIds"))
              + _looted_items(cur, params.get("LootedHeroItemTrapIds"), "trap_items"))
    # aufgesammelte Verbrauchsgüter stapeln wie Material
    items = [i for i in looted if i["$type"] != T("HeroConsumableItem")]
    dropped = [(i["TemplateId"], 1) for i in looted if i["$type"] == T("HeroConsumableItem")]
    # Der Client meldet das Schatzkammer-Item selbst zurück
    if (params.get("TreasureRoomHeroItem") and cur.get("treasure_item")
            and not cur.get("resurrections")):
        items.append(cur["treasure_item"])
    mines, per_mine, destroyed = _pillaged_mines(cur, params.get("PillagedMines"))
    mat_items, mat_lost, mat_destroyed = _pillaged_materials(cur, params.get("PillagedMines"))
    dropped += [(i["TemplateId"], 1) for i in mat_items]
    lost = _mine_loss(cur, per_mine)
    # Boosts auf Kills und Schatzkammer, nicht auf geplünderte Minen
    boosts = cur.get("boosts") or {}
    boost_gold = int((kills_gold + tr_gold) * boosts.get("Gold", 0))
    # Liga-Bonus auf Gold und Lebenskraft aus Kreaturen (Original: Teil von
    # KillsGold, der Bericht zieht LeagueBonusGold für den Tooltip ab)
    league_bonus = 0.0 if cur.get("practice") else trophies.attack_bonus(account)
    league_gold, league_lf = int(kills_gold * league_bonus), int(kills_lf * league_bonus)
    boost_lf = int((kills_lf + tr_lf) * boosts.get("LifeForce", 0))
    boost_xp = int(kills_xp * boosts.get("Xp", 0))
    gains = {"hero": cur.get("hero"), "items": items,
             "InGameCoin": (kills_gold + league_gold + tr_gold + boost_gold
                            + mines["InGameCoin"]),
             "LifeForce": kills_lf + league_lf + tr_lf + boost_lf + mines["LifeForce"],
             "PremiumCash": mines["PremiumCash"] + kills_blings + boss_blings,
             "bling_loot": kills_blings,
             "boss_bounty": boss_blings,
             "Xp": kills_xp + boost_xp,
             "castle_id": c.get("AccountId"),
             "validation": cur.get("validation", False),
             "practice": cur.get("practice", False),
             "delegated": cur.get("delegated", False),
             "completion_reward": cur.get("completion_reward") or [],
             "castle_type": castles.castle_type(c),
             "castle_level": c.get("Level", 1),
             "hero_level": hero["Level"] if hero else 1,
             "completion_type": params.get("CompletionType"),
             "competition": cur.get("competition", 0),
             "revenge": cur.get("revenge"),
             # Zeit bis zur Schatzkammer (Burgstatistik, mittlere Siegdauer)
             "stars": 0 if cur.get("practice") else stars(params, cur),
             "duration_ms": int(params.get("TreasureRoomOpenFrameCount", 0)
                                * params.get("AttackFrameDuration", 0) * 1000),
             "consumables_used": _potions_used(params, hero["Level"] if hero else 1),
             # Bonus-Zutaten aus den Ausrüstungseffekten (nicht im Test)
             "materials": dropped + ([] if cur.get("practice") or hero is None
                                     else item_drops.bonus_materials(
                                         hero, castles.castle_type(c) == castles.CASTLE_TYPE_USER)),
             "destroyed": _destroyed(c, params)}
    # Kronen nach den Sternen (0 Sterne = Niederlage), nur Spielerburgen,
    # nicht bei Übung und gezielten Angriffen (trophies.settle)
    trophy_change, trophy_defender, record = 0, 0, None
    if (castles.castle_type(c) == castles.CASTLE_TYPE_USER and not cur.get("practice")
            and not cur.get("targeted")):
        trophy_change, trophy_defender, record = trophies.settle(
            account, c, cur.get("hero"), gains["stars"])
    gains["trophies"] = trophy_change
    gains["trophy_record"] = record
    gains["defense"] = (None if cur.get("practice")
                        or castles.castle_type(c) != castles.CASTLE_TYPE_USER
                        else _defense(cur, params, per_mine, destroyed,
                                      {"InGameCoin": tr_gold, "LifeForce": tr_lf},
                                      trophy_defender, (mat_lost, mat_destroyed)))
    initial_xp = hero["XP"] if hero else 0
    prize = _roll_rating_prize() if c and not cur.get("practice") else None
    session.prize = prize
    session.rated_castle = c.get("AccountId")
    attack_id = cur.get("attack_id", "")
    session.current = {}  # jeder Angriff wird genau einmal abgerechnet
    return {
        "$type": T("EndAttackInfo"),
        "AttackId": attack_id,
        "BoostGold": boost_gold, "BoostLifeForce": boost_lf, "BoostXp": boost_xp,
        "CastleRatingFreePrize": prize,
        "CastleShieldedByAttacker": bool(gains["defense"] and gains["defense"]["shield"]),
        "CastleShieldedDuringAttack": cur.get("shielded", False),
        # Validierung: Zeit bis zur Schatzkammer; im Client int (ms, wie
        # CastleDetails), ein Kommawert ließ EndAttack scheitern
        "CastleValidationDuration": (gains["duration_ms"] if cur.get("validation")
                                     or cur.get("delegated") else 0),
        "CompletionReward": [],
        "CompletionType": params.get("CompletionType", 0),
        "DailyQuestGold": 0,
        "DeathCount": len(params.get("DeathDetails") or []),
        "DefenderAccountDisplayName": c.get("AccountDisplayName", ""),
        "DefenderCastleId": c.get("AccountId", 0),
        "DefenderCastleType": castles.castle_type(c),
        # Verlust des Besitzers: Minen und bei echten Spielern die
        # Schatzkammer (Anteil seines Wallets); generierte Beute zählt nicht
        "DefenderLossIGC": lost["InGameCoin"] + (tr_gold if "_Wallet" in c else 0),
        "DefenderLossLifeForce": lost["LifeForce"] + (tr_lf if "_Wallet" in c else 0),
        "DefenderOasisNameId": c.get("OasisNameId", 0),
        "Duration": params.get("Duration", 0),
        "EnterTreasureRoom": bool(params.get("TreasureRoomOpenFrameCount")),
        "HeroLevel": loot.level_for_xp(initial_xp + gains["Xp"]),
        "InitialGold": wallet["InGameCoin"],
        "InitialXp": initial_xp,
        "IsCastleAttackable": True,
        "IsCompletionRewardMissed": False,
        "IsNewUbisoftCompetitionLeader": False,
        "IsShielded": False,
        "IsTutorial": bool(c.get("IsTutorialCastle")),
        "IsUbisoftCompetitionValid": False,
        # wie im Original inklusive Boost und Liga-Bonus; der Tooltip im
        # Bericht zieht beide für die reine Kreaturen-Beute wieder ab
        "KillsGold": kills_gold + boost_gold + league_gold,
        "KillsLifeForce": kills_lf + boost_lf + league_lf,
        "KillsXp": kills_xp,
        "LastComment": None,
        "LeagueBonusGold": league_gold, "LeagueBonusLifeForce": league_lf,
        "LootTransferenceCost": 0,
        # beste Sterne in der Gewinn-Sperre (Spielerburgen), sonst diese
        "MaxVictoryConditionLevel": record["Max"] if record else gains["stars"],
        "PillagedIGCMineCount": 0, "PillagedIGCMinesAmount": mines["InGameCoin"],
        "PillagedLifeForceMineCount": 0,
        "PillagedLifeForceMinesAmount": mines["LifeForce"],
        "PillagedPremiumCashMineCount": 0,
        "PillagedPremiumCashMinesAmount": mines["PremiumCash"],
        "RevengeEnabled": False,
        "TotalGold": gains["InGameCoin"], "TotalLifeForce": gains["LifeForce"],
        "TotalPremiumCash": gains["PremiumCash"],
        "TotalXp": gains["Xp"],
        "TreasureRoomGold": tr_gold, "TreasureRoomLifeForce": tr_lf,
        "TreasureRoomReward": None,
        "TreasureRoomXp": 0,
        "TrophyScoreVariation": {"$type": T("TrophyScoreChange"),
                                 "TrophyScoreAttackerVariation": trophy_change,
                                 "TrophyScoreDefenderVariation": trophy_defender},
        "UpdatedAccountStats": account["Stats"],
        "VictoryConditionLevel": stars(params, cur),
        "VictoryConditionRewardRatios": [],
        "VictoryConditionType": 0,
    }, gains


def resurrect(session):
    """ResurrectionInfo; liefert (info, Gold-Kosten dieser Wiederbelebung)."""
    cur = session.current
    level = cur.get("hero_level", 1)
    n = cur.get("resurrections", 0)
    cost = resurrection_cost(level, n)
    cur["resurrections"] = n + 1
    return {
        "$type": T("ResurrectionInfo"),
        "NextResurrectionCost": resurrection_cost(level, n + 1),
        "NextTrophyScoreLost": 0,
        "ResurrectionCost": cost,
        "ResurrectionCount": n + 1,
    }, cost