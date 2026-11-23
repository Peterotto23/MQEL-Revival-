"""Persistenter Account-Zustand (ein Spieler) für den Revival-Server.

Startwert ist der generierte Stub (server/stubs/account-information.json,
Client-Form inkl. $type); danach gilt server/state/account.json. Der Client
meldet Aktionen als ServerCommands über ServerCommandService.SendCommands;
die bekannten werden hier auf den Zustand angewendet und sofort
gespeichert, unbekannte nur geloggt (sie bleiben dann client-lokal).

Löschen der Zustandsdatei (server/state/<profil>.json, s. PROFILES) setzt
den Account zurück.
"""

import collections
import copy
import datetime
import functools
import glob
import json
import math
import os
import random
import re
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

import achievements
import balance
import castle_sale
import castles
import competitions
import free_trials
import item_drops
import leagues
import loot
import objectives
import profiles
import special_packs
import tracking
import trophies

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Profil -> Vorlage für neue Accounts (server/stubs/); die Spieler liegen
# je Profil in server/state/<profil>/. "full": 4 Helden Lvl 30
# (Entwicklungsaccount), "fresh": 1 Ritter Lvl 5 aus "010031 - 1 Hero -
# Lvl 5" (Spielverlauf testen), "new": Neu-Account aus "000001 -
# DefaultAccount" (Tutorial von vorne)
PROFILES = {
    "full": "account-information.json",
    "fresh": "account-fresh.json",
    "new": "account-new.json",
}
SETTINGS = os.path.join(ROOT, "client", "GameData", "Data", "GameplaySettings")

CONTRACTS = "HyperQuest.GameServer.Contracts"


def T(name):
    return f"{CONTRACTS}.{name}, {CONTRACTS}"


# HeroItemSlot des Update3-Clients (ohne Finger2). Verifiziert per
# Ablegen aller Slots der Reihe nach im UI (Unequip SourceSlot 1,2,3,7,4,
# 11,5,6,8) und Kostüm -> 10; OffHand=9 ergibt sich aus der Lücke.
CLIENT_SLOTS = {1: "Head", 2: "Shoulders", 3: "Body", 4: "Back", 5: "Neck",
                6: "Finger1", 7: "Hands", 8: "MainHand", 9: "OffHand",
                10: "Costume", 11: "Pet"}

# Commands ohne Zustandsänderung (kein "nicht behandelt"-Log)
# SetLastViewedDefendLogCommand: Account hat kein Feld dafür (DefendLog nur
# Einträge + OfflinePeriod)
IGNORED = {"TrackingCommand", "ClientIdleCommand", "LogoutCommand",
           "SetLastViewedDefendLogCommand"}

@functools.cache
def _hero_spells(hero_spec):
    """[(Zauber-Id, [HeroMinimumLevel je Stufe])] eines Helden."""
    root = loot.SETTINGS
    path = glob.glob(os.path.join(root, "Heroes", f"{hero_spec:06d} - *",
                                  "Gameplay.json"))
    if not path:
        return ()
    with open(path[0], encoding="utf-8") as f:
        allowed = next((c["AllowedSpells"] for c in json.load(f)
                        if "AllowedSpells" in c), [])
    out = []
    for sid in allowed:
        p = glob.glob(os.path.join(root, "Abilities", f"{sid:06d} - *",
                                   "Gameplay.json"))
        if not p:
            continue
        with open(p[0], encoding="utf-8") as f:
            text = f.read()
        out.append((sid, [int(m) for m in
                          re.findall(r'"HeroMinimumLevel":\s*(\d+)', text)]))
    return tuple(out)


@functools.cache
def _equipment_spec(hero_spec):
    """HeroEquipmentDefinitionSpec aus Heroes/<Held>/Gameplay.json
    (DefaultEquipmentTemplateIds, HeroItemTypes)."""
    path = glob.glob(os.path.join(loot.SETTINGS, "Heroes", f"{hero_spec:06d} - *",
                                  "Gameplay.json"))
    if not path:
        return {}
    return next((c for c in _load_json(path[0]) if c["$type"].split(",")[0]
                 .endswith(".HeroEquipmentDefinitionSpec")), {})


@functools.cache
def _assignment_actions(assignment_id) -> tuple[dict[str, Any], ...]:
    path = glob.glob(os.path.join(loot.SETTINGS, "Assignments",
                                  f"{assignment_id:06d} - *", "Gameplay.json"))
    if not path:
        return ()
    with open(path[0], encoding="utf-8") as f:
        spec = json.load(f)
    return tuple((spec[0] if isinstance(spec, list) else spec).get("Actions", []))


def _assignment_action(assignment_id, index) -> dict[str, Any] | None:
    actions = _assignment_actions(assignment_id)
    return actions[index] if 0 <= index < len(actions) else None


# HeroItems/InventorySettings.InventorySlotByTabCount
INVENTORY_SLOTS_PER_TAB = 21

# CurrencyType -> Wallet-Feld
CORPSE_AMOUNT = {"$type": T("CurrencyAmount"), "Amount": 0, "CurrencyType": 4}
NO_EXPIRABLE = "000000000000000000000000"  # leere MongoDB-ObjectId
# Quelle für das Bling-Kontobuch je Thread (= laufende Anfrage), gesetzt vom
# Spielserver bzw. Admin-Dienst; gilt auch für fremde Accounts, die die
# Anfrage ändert (z. B. geplünderte Mine)
SOURCE = threading.local()

CURRENCY = {1: "PremiumCash", 2: "InGameCoin", 4: "LifeForce"}


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


MAX_INVENTORY_TABS = 8


class GameData:
    """Lesezugriff auf die benötigten GameplaySettings (lazy)."""

    def __init__(self):
        self._skus = None
        self._templates = None
        self._consumables = None

    @property
    def skus(self):
        if self._skus is None:
            s = _load_json(os.path.join(SETTINGS, "ShopSettings",
                                        "ShopSkuBaseSettings.json"))
            self._skus = {x["Code"]: x for x in s["Skus"]}
        return self._skus

    @property
    def consumables(self):
        if self._consumables is None:
            c = _load_json(os.path.join(SETTINGS, "GeneralSettings",
                                        "ConsumableSettings.json"))
            self._consumables = {x["TemplateId"]: x for x in c["TemplateList"]}
        return self._consumables

    @functools.cached_property
    def forge(self):
        return _load_json(os.path.join(SETTINGS, "GeneralSettings", "ForgeSettings.json"))

    @functools.cached_property
    def time(self):
        return _load_json(os.path.join(SETTINGS, "GeneralSettings", "TimeSettings.json"))

    @functools.cached_property
    def inventory(self):
        return _load_json(os.path.join(SETTINGS, "HeroItems", "InventorySettings.json"))

    @functools.cached_property
    def shop(self):
        return _load_json(os.path.join(SETTINGS, "GeneralSettings", "ShopSettings.json"))

    @property
    def templates(self):
        if self._templates is None:
            t = _load_json(os.path.join(SETTINGS, "HeroItems",
                                        "HeroItemTemplates.json"))
            self._templates = {x["Id"]: x for x in t["TemplateList"]}
        return self._templates

    @functools.cached_property
    def cosmetic_item_types(self):
        """HeroItemTypeIds der Kosmetik (Kostüme, Pets): keine Werte,
        Levelspanne nur Freischaltung."""
        t = _load_json(os.path.join(SETTINGS, "HeroItems", "HeroItemTypes.json"))
        return {x["Id"] for x in t["ItemTypeList"]
                if x.get("HeroItemCategoryType") in ("Costume", "Pet")}

    def is_cosmetic(self, template):
        return template.get("HeroItemTypeId") in self.cosmetic_item_types


NEWS_MAX = 15  # NewsSettings.NewsMaxCount
NEWS_TIMEOUT = datetime.timedelta(weeks=2)  # NewsSettings.NewsTimeout "2w"
MAXIMUM_DEFEND_LOG = 100  # BattleLogSettings.MaximumDefendLog


class AccountState:
    def __init__(self, state_path, log=print, profile="full", account_id=None,
                 country=None):
        self.seed = os.path.join(ROOT, "server", "stubs", PROFILES[profile])
        self.state = state_path
        self.lock = threading.Lock()
        self.pack_events = []  # Meldungen aus Pack-Käufen (take_pack_events)
        self.events = []  # (NotificationType-Name, Felder) aus Commands
        self.overflow = []  # BuyBackAdded für Beute bei vollem Inventar, noch nicht gemeldet
        self.spells_unlocked = []  # durch Levelaufstieg, noch nicht gemeldet
        self.track: Callable[..., Any] | None = None  # (TrackingTagId, Felder) -> Tracking, setzt players.Player
        self.data = GameData()
        self.log = log
        # Kontobuch der Blings (docs/70-bling-wirtschaft.md): jede Änderung
        # mit Quelle (SOURCE der laufenden Anfrage, Commands mit Namen)
        self.ledger = os.path.splitext(state_path)[0] + ".blings.jsonl"
        self._blings = None
        self._kinds = {}  # Bling-Quellen seit der letzten Buchung (_tag)
        if os.path.exists(self.state):
            self.account = _load_json(self.state)
        else:
            self.account = _load_json(self.seed)
            if account_id is not None:  # neuer Account aus der Vorlage
                self.account["AccountId"] = account_id
                self.account["BuildInfo"]["Draft"]["AccountId"] = account_id
            if country:  # sonst der Standard der Vorlage (DE)
                self.account["CountryCode"] = country
            self._save()
        self._blings = self.account["Wallet"].get("PremiumCash", 0)
        if (self._unlock_equipped_spells() | bool(self._unlock_spells())
                | bool(self._pack_heroes()) | self._complete_attack_regions()
                | self._grant_castle_shareable() | self._clear_empty_forge()
                | self._migrate_last_viewed() | self._init_league()
                | self._drop_orphan_shields() | self._sync_belts()
                | self._drop_default_equipment() | self._fix_tracking_intervals()
                | self._fix_tutorial_level() | self._fix_pack_item_levels()):
            self._save()

    def fix_tutorial_level(self):
        """Bei GetAccountInformation (Spielstart): Helden, der im Tutorial
        festhängt, auf Level 2 heben (siehe _fix_tutorial_level)."""
        with self.lock:
            if self._fix_tutorial_level():
                self._save()
                self.log("    [state] Tutorial: Held auf Level 2 gehoben")

    def _fix_tutorial_level(self):
        """Nach beiden Tutorial-Burgen (Assignment 150) verlangt 125 "Equip a
        skill 2" einen Zauber, den es erst ab Heldenlevel 2 gibt; wer dort
        mit Level 1 ankam, hing fest (auch nach Neustart). Das passiert, wenn
        der Server mitten in einer Tutorial-Burg neu startet: die Beute-
        tabelle des Angriffs ist dann weg, EndAttack bringt 0 XP. Held auf
        Level 2."""
        done = self.account.get("CompletedAssignments") or []
        hero = next((h for h in self.account.get("Heroes") or []
                     if h["HeroSpecContainerId"] == self.account.get("SelectedHeroId")), None)
        if 150 not in done or 125 in done or hero is None or hero["Level"] >= 2:
            return False
        hero["XP"] = max(hero["XP"], loot.xp_per_level()[1])
        hero["Level"] = 2
        self._unlock_spells()
        return True

    def _fix_tracking_intervals(self):
        """Tracking-Intervalle in Sekunden (der Stub hatte bis zu diesem Fix
        Millisekunden: 60000/300000 statt 60/300)."""
        t = (self.account.get("ClientSettings") or {}).get("ClientTrackingSettings") or {}
        fixed = False
        for key, sec in (("ClientIdleInterval", 60), ("GameStateTrackingInterval", 300)):
            if t.get(key) == sec * 1000:
                t[key] = sec
                fixed = True
        return fixed

    def _drop_default_equipment(self):
        """Standard-Ausrüstung gehört nie in die Taschen (bis zu diesem Fix
        landete sie beim Tausch dort, der Client zeigte sie als nicht
        tragbar): raus aus den Taschen, leere Default-Slots neu belegen."""
        defaults = set()
        changed = False
        for hero in self.account["Heroes"]:
            for slot, tid in self._default_equipment(hero["HeroSpecContainerId"]).items():
                defaults.add(tid)
                if not hero["Equipment"].get(slot):
                    hero["Equipment"][slot] = self._item_from_template(tid)
                    changed = True
        slots = self._slots()
        keep = [x for x in slots if x["Item"].get("TemplateId") not in defaults
                or "Consumable" in x["Item"].get("$type", "")]
        if len(keep) != len(slots):
            slots[:] = keep
            changed = True
        return changed

    def _fix_pack_item_levels(self):
        """Kosmetik (Pets, Kostüme, ...) kam früher auf LevelMax ins Inventar
        (gesperrt für Helden darunter, Anlegen vom Server abgelehnt): auf
        LevelMin wie neu gekaufte (siehe _item_from_template)."""
        fixed = False
        items = [(s, "Item") for s in self._slots()] + [
            (h["Equipment"], slot) for h in self.account.get("Heroes") or []
            for slot in h.get("Equipment") or {}]
        for holder, key in items:
            item = holder[key]
            t = item and self.data.templates.get(item.get("TemplateId"))
            if t and self.data.is_cosmetic(t) and item.get("ItemLevel", 1) > t.get("LevelMin", 1):
                holder[key] = self._item_from_template(item["TemplateId"])
                fixed = True
        return fixed

    def _migrate_last_viewed(self):
        """Altes Feld LastShownWelcomePageDate -> LastViewedDates (Type 1);
        Einträge mit falschem $type LastViewedDate korrigieren."""
        fixed = False
        for x in self.account.get("LastViewedDates") or []:
            if x.get("$type") != T("ViewableItem"):
                x["$type"] = T("ViewableItem")
                fixed = True
        date = self.account.pop("LastShownWelcomePageDate", None)
        if date and not self.account.get("LastViewedDates"):
            self.account["LastViewedDates"] = [{"$type": T("ViewableItem"), "Type": 1,
                                                "LastViewedDate": date}]
        return fixed or date is not None

    def _clear_empty_forge(self):
        """Leere Schmiede = Inventory.ForgedItem null. Steht dort ein Objekt,
        hält das Schmiede-Fenster (0xca5540) es für laufend und liest
        ForgedItem.Item -> Absturz bei Item null."""
        inv = self.account["Inventory"]
        forged = inv.get("ForgedItem")
        if forged and not forged.get("Item"):
            inv["ForgedItem"] = None
            return True
        # Held ohne Waffe (Umschmieden der MainHand bis 4e78454): das
        # geschmiedete Teil sofort in die Hand, Schmiede leeren
        bare = [h for h in self.account["Heroes"]
                if h["HeroSpecContainerId"] == self.account.get("SelectedHeroId")
                and (h.get("Equipment") or {}).get("MainHand") is None]
        if forged and bare:
            bare[0]["Equipment"]["MainHand"] = forged["Item"]
            self.account["Expirables"] = [e for e in self.account.get("Expirables") or []
                                          if e["Id"] != forged["ExpirableId"]]
            inv["ForgedItem"] = None
            return True
        # ForgedItem ohne Expirable (bis 062d8d5 beim Fertigwerden gelöscht):
        # fällig wieder anlegen, der Client meldet es dann als abgelaufen
        expirables = self.account.setdefault("Expirables", [])
        if forged and not any(e["Id"] == forged["ExpirableId"] for e in expirables):
            expirables.append({"$type": T("ForgeExpirable"), "Id": forged["ExpirableId"],
                               "ExpirableType": "Forge", "ForgeMode": forged["ForgeMode"],
                               "DueDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return True
        return False

    def _unlock_equipped_spells(self):
        """Ausgerüstete Zauber müssen freigeschaltet sein.

        Die AccountTemplates haben UnlockedSpells leer; ohne Eintrag meldet
        der Client im Angriff "Spell not found" / "Invalid ability level -1".
        Level 1, bis die Spell-Specs (Max-Level) ausgewertet sind.
        """
        unlocked = self.account.setdefault("UnlockedSpells", [])
        have = {s["SpellSpecContainerId"] for s in unlocked}
        added = False
        for hero in self.account["Heroes"]:
            for slot in hero.get("EquippedSpells") or []:
                sid = slot["SpellSpecContainerId"]
                if sid not in have:
                    unlocked.append({"$type": T("Spell"), "Level": 1,
                                     "NewlyAdded": False,
                                     "SpellSpecContainerId": sid})
                    have.add(sid)
                    added = True
        return added

    def _unlock_spells(self):
        """Zauber nach Heldenlevel freischalten bzw. aufwerten.

        Heroes/<Held>: HeroSpellsSpec.AllowedSpells; Abilities/<Zauber>:
        Levels[].HeroMinimumLevel. Zauber-Level = Anzahl erreichter Stufen.
        Liefert die geänderten Spell-Einträge (für SpellUnlocked)."""
        unlocked = self.account.setdefault("UnlockedSpells", [])
        by_id = {s["SpellSpecContainerId"]: s for s in unlocked}
        changed = []
        for hero in self.account["Heroes"]:
            for sid, mins in _hero_spells(hero["HeroSpecContainerId"]):
                level = sum(1 for m in mins if m <= hero["Level"])
                if level == 0:
                    continue
                spell = by_id.get(sid)
                if spell is None:
                    spell = by_id[sid] = {"$type": T("Spell"), "Level": 0,
                                          "NewlyAdded": True,
                                          "SpellSpecContainerId": sid}
                    unlocked.append(spell)
                if level > spell["Level"]:
                    spell["Level"] = level
                    changed.append(copy.deepcopy(spell))
                    self.log(f"    [state] Zauber {sid} -> Level {level}")
        return changed

    # -- Persistenz ---------------------------------------------------------

    def _sync_belts(self):
        """Gürtel aller Helden (EquippedConsumables.StackCount) = Vorrat des
        Tranks im gemeinsamen Inventar. Der Gürtel verweist nur auf die
        Inventarstapel; ein veralteter Stand (0 trotz Vorrat) ließ im Kampf
        keine Tränke zu, während die Warnung vorher das Inventar prüft."""
        stock = {}
        for slot in self.account["Inventory"]["HeroItems"]:
            if slot["Item"].get("$type") == T("HeroConsumableItem"):
                tid = slot["Item"]["TemplateId"]
                stock[tid] = stock.get(tid, 0) + slot.get("StackCount", 1)
        changed = False
        for hero in self.account.get("Heroes") or []:
            for belt in hero.get("EquippedConsumables") or []:
                n = stock.get(belt["TemplateId"], 0)
                changed |= belt["StackCount"] != n
                belt["StackCount"] = n
        return changed

    def _book(self, source):
        """Bling-Änderung seit der letzten Buchung ins Kontobuch."""
        now = self.account["Wallet"].get("PremiumCash", 0)
        if self._blings is not None and now != self._blings:
            os.makedirs(os.path.dirname(self.ledger), exist_ok=True)
            with open(self.ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "Time": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "Delta": now - self._blings, "Balance": now,
                    "Source": source or "server",
                    **({"Kinds": self._kinds} if self._kinds else {})}) + "\n")
        self._blings = now
        self._kinds = {}

    def _tag(self, kind, delta):
        """Bling-Änderung einer Quelle zuordnen (Kontobuch "Kinds", für die
        Telemetrie im Webtool); Rest einer Buchung ohne Quelle bleibt offen."""
        if delta:
            self._kinds[kind] = self._kinds.get(kind, 0) + delta

    def _save(self):
        self._sync_belts()  # Invariante vor jedem Speichern
        self._book(getattr(SOURCE, "value", None))
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        tmp = self.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.account, f, ensure_ascii=False)
        os.replace(tmp, self.state)  # atomar: nie halb geschriebener Zustand

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.account)

    def read(self, fn):
        """fn(Account) unter der Sperre, ohne Kopie (für Übersichten über
        viele Accounts); fn darf nichts ändern und nichts davon behalten."""
        with self.lock:
            return fn(self.account)

    # -- Special Packs ------------------------------------------------------

    # Verbrauchte Pack-Kontingente {SkuCode: Anzahl}; nur serverseitig,
    # GetAccountInformation entfernt das Feld
    CONSUMED = "ShopSkuConsumed"

    def grant_special_pack(self, pack_id):
        """Pack freischalten (einmalig); False, wenn schon vorhanden.
        Meldungen zu Ausrüstung/Material holt take_pack_events."""
        with self.lock:
            if not self._grant_special_pack(pack_id):
                return False
            self._save()
            return True

    def buy_special_pack(self, pack_id, price):
        """Pack gegen `price` Blings (Bling-Shop): "owned", "blings" (zu
        wenig) oder "ok"; Abbuchen und Freischalten in einem Schritt."""
        with self.lock:
            if pack_id in self.account.get("SpecialPacks", []):
                return "owned"
            wallet = self.account["Wallet"]
            if wallet["PremiumCash"] < price:
                return "blings"
            wallet["PremiumCash"] -= price
            self._tag("bling shop", -price)
            self.pack_events.insert(0, ("WalletUpdated", {"Amounts": [
                {"$type": T("CurrencyAmount"), "Amount": -price, "CurrencyType": 1}],
                "UpdateTag": 0}))
            self._grant_special_pack(pack_id)
            self._save()
            return "ok"

    def take_pack_events(self):
        """Meldungen aus Pack-Käufen ([(Typ, Felder)]) für die Notifications."""
        with self.lock:
            out, self.pack_events = self.pack_events, []
            return out

    def _grant_special_pack(self, pack_id):
        packs = self.account.setdefault("SpecialPacks", [])
        if pack_id in packs:
            return False
        packs.append(pack_id)
        self.account["BuildInfo"].setdefault("OwnerSpecialPacks", []).append(pack_id)
        # Zusätzliche Inventar-Reiter; 9 (2 + 7 des Infinite Packs) ließ
        # den Client beim Öffnen des Inventars abstürzen, daher gedeckelt
        # (8 = höchster Wert der Original-Account-Vorlagen)
        pack = special_packs.pack(pack_id)
        extra = pack.get("AdditionalInventoryTabCount", 0)
        inv = self.account["Inventory"]
        inv["InventoryTabCount"] = min(MAX_INVENTORY_TABS,
                                       inv["InventoryTabCount"] + extra)
        self._pack_heroes()
        self._pack_castle_items(pack)
        self._pack_inventory_items(pack)
        self.log(f"    [state] Special Pack {pack_id} freigeschaltet")
        return True

    def _pack_inventory_items(self, pack):
        """Ausrüstung (HeroItems) und Material (CraftingMaterials) eines
        Packs ins Inventar; ohne Platz kostenlos in den Rückkauf."""
        occupied = self._occupied()
        # Ausrüstung aus dem Template (Pack-Einträge nennen oft nur TemplateId,
        # dann Level 1 wie in den Original-Vorlagen; LevelMax 30 sperrte Pets
        # und Kostüme für niedrigere Helden), Verbrauchsgüter
        # (HeroConsumableItem mit StackCount) wie Material
        rewards = [("consumable", x["TemplateId"], x.get("StackCount", 1))
                   if x["$type"] == T("HeroConsumableItem")
                   else ("equipment", x["TemplateId"], x.get("ItemLevel", 1))
                   for x in pack.get("HeroItems", [])]
        rewards += [("consumable", m["Id"], m.get("Quantity", 1))
                    for m in pack.get("CraftingMaterials", [])]
        slots = self._add_reward_items(rewards)
        self.pack_events += (self._inventory_events(
            list({x["SlotIndex"]: x for x in slots}.values()), occupied)
            + self._take_overflow())

    def _pack_castle_items(self, pack):
        """Burg-Inhalt eines Packs (z. B. Halloween Pack 67): Kreaturen,
        Fallen und Deko in den Vorrat, Themes (UnlockedThemes) frei."""
        for kind in ("Creature", "Trap"):
            for x in pack.get(kind + "s", []):
                self._add_archetype(kind, x["SpecContainerId"], x.get("AvailableCount", 1))
        for x in pack.get("Decorations", []):
            for _ in range(x.get("AvailableCount", 1)):
                self._add_inventory_decoration(x["SpecContainerId"])
        themes = self.account["BuildInfo"].setdefault("InventoryThemes", [])
        themes += [t for t in pack.get("UnlockedThemes", []) if t not in themes]

    def _grant_castle_shareable(self):
        """AccountPrivilege CastleShareable (32) zusammen mit dem Burgbau
        (256, Assignment 5006). Ohne das Recht sperrt der Client den Knopf
        "Burg testen" (BuildController 0xd559ac: IsCastleShareable =
        Privileges-Bit 5) und die Burg lässt sich nie validieren. Keine
        Assignment-Aktion vergibt es; die FullAccount-Vorlagen haben 489."""
        p = self.account.get("Privileges", 0)
        if p & 256 and not p & 32:
            self.account["Privileges"] = p | 32
            return True
        return False

    def _complete_attack_regions(self):
        """Friends (1) und Competitions (2) muss jeder Held in
        Hero.AttackRegions haben: der Client sucht sie dort ohne Prüfung
        (GetCurrentHeroAttackRegions, 0xcee2b0) und stürzt sonst ab.
        Gesperrte Regionen fehlen besser ganz: mit Locked-Einträgen (4-9)
        wählte der Client beim Wechsel in den Angriff Region 9."""
        path = os.path.join(loot.SETTINGS, "GeneralSettings", "AttackAdvisorSettings.json")
        free = [r["AttackRegionId"] for r in _load_json(path)["AttackRegions"]
                if r.get("IsFriendRegion") or r.get("IsCompetitionRegion")]
        changed = False
        for owner in [self.account, *self.account["Heroes"]]:
            regions = [r for r in owner.get("AttackRegions") or [] if r["Status"] != 1]
            have = {r["AttackRegionId"] for r in regions}
            regions += [{"$type": T("AccountAttackRegion"), "AttackRegionId": rid,
                         "Status": 2} for rid in free if rid not in have]
            if regions != owner.get("AttackRegions"):
                owner["AttackRegions"] = regions
                changed = True
        return changed

    def _pack_heroes(self):
        """Helden aus EarlyHeroAccess der Packs anlegen. Ohne sie zeigt der
        Client die Helden als verfügbar an, findet sie im Account aber
        nicht (Assert "Cannot find Archetype for Armor # 0" im Shop)."""
        owned = {h["HeroSpecContainerId"] for h in self.account["Heroes"]}
        added = []
        for pid in self.account.get("SpecialPacks", []):
            for spec_id in special_packs.pack(pid).get("EarlyHeroAccess", []):
                if spec_id not in owned:
                    self.account["Heroes"].append(self._new_hero(spec_id))
                    owned.add(spec_id)
                    added.append(spec_id)
        if added:
            self.spells_unlocked += self._unlock_spells()
            self.log(f"    [state] Helden aus Special Pack: {added}")
        return added

    # Gekaufte Startburg (SaleId); nur serverseitig wie CONSUMED
    SALE = "CastleSaleId"
    # Letzte Produktionsabrechnung je Mine {BuildingId: Unix-Zeit}; nur serverseitig
    MINE_CLOCK = "MineClock"
    # Umschmieden der Waffe {Hero, Slot (Client-Nummer)}: beim Abholen
    # ersetzt; nur serverseitig
    FORGE_MAIN_HAND = "ForgeMainHand"

    def _castle_sale_id(self):
        """SaleId der Startburg; ältere Spielstände: über das Burgthema."""
        sale = self.account.get(self.SALE)
        if sale is None:
            sale = castle_sale.sale_for_theme(self.account["BuildInfo"]["Draft"].get("ThemeId"))
        return sale

    def shop_sku_modifiers(self):
        return special_packs.modifiers(
            self.account.get("SpecialPacks", []), self.account["BuildInfo"]["Level"],
            self.account.get(self.CONSUMED, {}),
            special_packs.castle_level_packs(self._castle_sale_id()))

    def _consume_limited(self, sku):
        """Pack-Kontingent prüfen und verbuchen (vor dem Kauf aufrufen)."""
        code = sku["Code"]
        mod = next((m for m in self.shop_sku_modifiers() if m["SkuCode"] == code), None)
        consumed = self.account.setdefault(self.CONSUMED, {})
        if mod is None:
            if sku.get("IsLocked"):
                raise ValueError(f"SKU {code} gesperrt (Pack fehlt)")
            if not sku.get("IsLimitedQuantity"):
                return
            # ohne Pack gilt das Kontingent der SKU selbst (Pack-SKUs wie
            # 15722 haben keins, also 0)
            mod = {"LimitedQuantityConsumed": consumed.get(code, 0),
                   "LimitedQuantityMax": sku.get("LimitedQuantityMax", 0)}
        if mod["LimitedQuantityConsumed"] >= mod["LimitedQuantityMax"]:
            raise ValueError(f"SKU {code}: Kontingent erschöpft")
        consumed[code] = consumed.get(code, 0) + 1

    # -- Commands -----------------------------------------------------------

    # Änderungen am Entwurf, nach denen die Burg neu validiert werden muss
    # (Original: CastleManager/BuildingManager setzen IsDraftValidated =
    # false); ohne das meldete GetAccountInformation sie nach dem Neustart
    # wieder als validiert
    DRAFT_COMMANDS = frozenset(
        f"{verb}Castle{kind}Command" for verb in ("Add", "Update", "Remove")
        for kind in ("Room", "Creature", "Trap", "Decoration", "Trigger")
    ) | {"UpdateBuildingCommand"}

    def apply(self, commands):
        """Wendet eine SendCommands-Liste an; speichert bei Änderungen.

        Liefert die dabei entstandenen Server-Notifications als
        [(Typ-Name, Felder)]."""
        changed = False
        with self.lock:
            self.events = []
            for cmd in commands or []:
                name = cmd.get("$type", "").split(",")[0].split(".")[-1]
                handler = getattr(self, "_cmd_" + name, None)
                if handler is None:
                    tag = (cmd.get("TrackingTag") or {}).get("TrackingTagId")
                    if name == "TrackingCommand" and tag in tracking.CLIENT_COMMAND_TAGS and self.track:
                        self.track(tag, dict(cmd["TrackingTag"]))
                    elif name not in IGNORED:
                        self.log(f"    [state] nicht behandelt: {name} {cmd}")
                    continue
                tag = tracking.COMMAND_TAGS.get(name) if self.track else None
                before = {f: self.account["Wallet"].get(f, 0) for f in tracking.WALLET_FIELDS}
                try:
                    handler(cmd)
                    changed = True
                    if name in self.DRAFT_COMMANDS:
                        self.account["BuildInfo"]["IsDraftPublished"] = False
                    self._book(f"{getattr(SOURCE, 'value', None) or ''} cmd:{name}")
                    if tag and self.track:
                        delta = {f: self.account["Wallet"].get(f, 0) - v
                                 for f, v in before.items()}
                        self.track(tag, dict(cmd, Command=name, WalletDelta=delta))
                except Exception as e:  # noqa: BLE001 - ein kaputtes Command killt nie den Server
                    self.log(f"    [state] {name} fehlgeschlagen: {e!r}")
            if changed:
                self._settle_forged_weapon()
                # Bau-Quests: der Client hat sie schon abgehakt und die
                # Belohnung lokal gebucht, daher keine Meldungen
                self._complete_objectives(objectives.completed_by_build(self.account),
                                          self.account.get("SelectedHeroId"))
                self._check_achievements()
                self._save()
            return self.events + self._take_overflow()

    def spend(self, field, amount):
        """Bucht `amount` vom Wallet-Feld ab (nie unter 0) und speichert."""
        with self.lock:
            wallet = self.account["Wallet"]
            old = wallet[field]
            wallet[field] = max(0, old - amount)
            self._save()
            return {field: wallet[field] - old}

    def _credit(self, field, amount):
        """Gutschrift, gedeckelt auf die Lagerkapazität (Gold/Lebenskraft;
        wie im Spiel geht der Überschuss verloren)."""
        wallet = self.account["Wallet"]
        cap = wallet.get(field + "StorageCapacity")
        new = wallet[field] + amount
        if cap is not None and new > cap:
            if amount:
                self.log(f"    [state] Lager voll: {field} auf {cap} begrenzt")
            new = max(wallet[field], cap)
        delta = new - wallet[field]
        wallet[field] = new
        return delta

    def earn(self, field, amount):
        """Schreibt `amount` dem Wallet-Feld gut, speichert; liefert die
        tatsächliche Änderung {Feld: Delta} (nach Lager-Deckelung)."""
        with self.lock:
            delta = self._credit(field, amount)
            self._save()
            return {field: delta}

    def admin_wallet(self, deltas):
        """Wallet-Korrektur aus dem Admin-Werkzeug: {Feld: Delta}, ohne
        Lager-Deckelung, nie unter 0. Liefert die tatsächlichen Deltas."""
        with self.lock:
            wallet = self.account["Wallet"]
            out = {}
            for field, d in deltas.items():
                old = wallet.get(field, 0)
                wallet[field] = max(0, old + int(d))
                if wallet[field] != old:
                    out[field] = wallet[field] - old
            self._save()
            return out

    # Sanktion aus dem Admin-Werkzeug {"Status": 1 gesperrt | 2 gebannt
    # (AccountStatus), "Until": ISO-Zeit bei Sperre, "Reason"}; nur
    # serverseitig, GetAccountInformation entfernt das Feld
    SANCTION = "Sanction"

    def sanction(self, now):
        """Aktive Sanktion oder None; abgelaufene Sperre wird gelöscht
        (wie ConnectionManager.EnsureTokenIsValid)."""
        with self.lock:
            s = self.account.get(self.SANCTION)
            if s and s["Status"] == 1 and s["Until"] <= now.strftime("%Y-%m-%dT%H:%M:%SZ"):
                del self.account[self.SANCTION]
                self._save()
                return None
            return copy.deepcopy(s) if s else None

    def set_sanction(self, sanction):
        """Sanktion setzen (None = aufheben)."""
        with self.lock:
            if sanction:
                self.account[self.SANCTION] = sanction
            else:
                self.account.pop(self.SANCTION, None)
            self._save()

    def admin_hero_level(self, hero_id, level):
        """Held per Admin-Werkzeug auf `level` heben (XP bis zur Schwelle;
        nie abwärts). Liefert (Held, XP-Zuwachs, Level geändert)."""
        with self.lock:
            hero = self._hero(hero_id)
            table = loot.xp_per_level()
            if not 1 <= level <= len(table):
                raise ValueError(f"Level {level} außerhalb 1..{len(table)}")
            xp = max(0, table[level - 1] - hero["XP"])
            hero, changed = self._add_xp(hero_id, xp)
            self._save()
            return hero, xp, changed

    def admin_give(self, items):
        """Items aus dem Admin-Werkzeug: [("equipment", TemplateId, Level) |
        ("material", TemplateId, Anzahl)]; ohne Platz in den Rückkauf.
        Liefert die Inventar-Meldungen."""
        with self.lock:
            occupied = self._occupied()
            slots = self._add_reward_items(items)
            self._save()
            return self._inventory_events(slots, occupied) + self._take_overflow()

    def credit_attack(self, gains):
        """Bucht die Beute eines Angriffs: Wallet + Helden-XP/Level.

        Liefert, was sich geändert hat (für die Notifications):
        {"wallet": {Feld: Delta}, "inventory": [(Meldung, Felder)], "hero",
        "level_changed"}.
        """
        with self.lock:
            wallet = {field: self._credit(field, gains.get(field, 0))
                      for field in ("InGameCoin", "LifeForce", "PremiumCash")}
            loot.record_bling_loot(self.account, gains.get("bling_loot", 0), time.time())
            loot.record_boss_bounty(self.account, gains.get("boss_bounty", 0), time.time())
            blings = wallet["PremiumCash"]
            from_loot = min(blings, gains.get("bling_loot", 0))
            from_boss = min(blings - from_loot, gains.get("boss_bounty", 0))
            self._tag("loot", from_loot)
            self._tag("boss", from_boss)
            self._tag("pillage", blings - from_loot - from_boss)
            potions = []
            # Validierungslauf verbraucht keine Tränke (Hinweis im Client)
            if gains["hero"] is not None and not gains.get("validation"):
                potions = self._use_consumables(gains["hero"], gains.get("consumables_used"))
            occupied = self._occupied()
            # Angriffsbeute ohne Platz geht verloren (Client warnt vorher:
            # "Items that can't be looted during the attack will be lost")
            slots = self._add_items(gains.get("items") or [], keep_overflow=False)
            slots += self._add_reward_items(
                [("material", tid, n) for tid, n in gains.get("materials") or []],
                keep_overflow=False)
            hero, level_changed = None, False
            if gains["hero"] is not None and gains["Xp"]:
                hero, level_changed = self._add_xp(gains["hero"], gains["Xp"])
            if gains.get("stars", 0) > self.castle_stars(gains["castle_id"]):
                self.account.setdefault(self.STARS, {})[str(gains["castle_id"])] = gains["stars"]
            self._save()
            return {"wallet": wallet,
                    "inventory": potions + self._inventory_events(slots, occupied)
                    + self._take_overflow(),
                    "hero": hero, "level_changed": level_changed}

    def record_attack(self, gains, params):
        """Angriff in Statistik und Errungenschaften zählen (nicht bei
        Validierung und Test-Angriff). Liefert die Meldungen [(Typ, Felder)]."""
        with self.lock:
            if gains["practice"]:
                return []
            self.events = []
            achievements.record_attack(self.account, gains, params)
            self._check_achievements()
            self._save()
            return self.events

    def add_friend(self, summary):
        """Freund (AccountSummary; AccountSummaryForFriend kennt der finale
        Client nicht mehr) eintragen; liefert die Meldungen
        (FriendshipAdded, ggf. Errungenschaften)."""
        with self.lock:
            friends = self.account.setdefault("Friends", [])
            if any(f["Id"] == summary["Id"] for f in friends):
                return []
            friends.append(summary)
            self.events = [("FriendshipAdded", {"FriendAccountSummary": summary})]
            self._check_achievements()
            self._save()
        self.log(f"    [state] Freund {summary['DisplayName']} ({summary['Id']})")
        return self.events

    def add_invitation(self, invitation):
        """Offene Freundschaftseinladung (FriendshipInvitation) eintragen,
        eine ältere mit demselben Spieler ersetzen."""
        with self.lock:
            invs = [i for i in self.account.get("FriendshipInvitations") or []
                    if i["FriendAccountId"] != invitation["FriendAccountId"]]
            self.account["FriendshipInvitations"] = invs + [invitation]
            self._save()

    def take_invitation(self, friend_id):
        """Offene Einladung mit friend_id austragen; liefert sie oder None."""
        with self.lock:
            invs = self.account.get("FriendshipInvitations") or []
            found = next((i for i in invs if i["FriendAccountId"] == friend_id), None)
            if found:
                self.account["FriendshipInvitations"] = [i for i in invs if i is not found]
                self._save()
            return found

    def remove_friend(self, friend_id):
        """Freund austragen, samt seiner Freundes-News
        (NewsManager.OnFriendshipRemoved)."""
        with self.lock:
            friends = self.account.setdefault("Friends", [])
            kept = [f for f in friends if f["Id"] != friend_id]
            if len(kept) == len(friends):
                return
            self.account["Friends"] = kept
            box = self.account.get("News")
            if box:
                box["News"] = [n for n in box.get("News") or []
                               if n["Data"].get("FriendAccountId") != friend_id]
            self._save()
        self.log(f"    [state] Freund {friend_id} entfernt")

    DATES = achievements.DATES

    # Eigene Burg-Bewertungen {Burg-Id: Sterne} (CastleInfo.LastCastleRating);
    # nur serverseitig, GetAccountInformation entfernt das Feld
    RATINGS = "CastleRatings"

    # Beste erzielte Sterne je Burg {Burg-Id: Sterne}
    # (CastleInfo.VictoryConditionLevel); nur serverseitig wie RATINGS
    STARS = "CastleStars"
    # Heldenleichen: {EntityHarvestId: {Attacker, Died, Materials}} (nur Server)
    CORPSES = "HeroCorpseLoot"

    def castle_stars(self, castle_id):
        return (self.account.get(self.STARS) or {}).get(str(castle_id), 0)

    def rate_castle(self, castle_id, rating):
        with self.lock:
            self.account.setdefault(self.RATINGS, {})[str(castle_id)] = rating
            self._save()
        self.log(f"    [state] Burg {castle_id} mit {rating} Sternen bewertet")

    PLAY_TIME = "PlaySeconds"  # {Datum: Sekunden}, nur serverseitig (Telemetrie)
    PLAY_GAP = 600  # längere Pausen zählen nicht als Spielzeit
    PLAY_DAYS = 90

    def seen(self, now):
        """LastSeenOnline (Burgvorschau anderer Spieler); gespeichert
        höchstens alle 5 Minuten. Die Zeit seit dem letzten Stand zählt als
        Spielzeit, wenn die Lücke höchstens PLAY_GAP ist."""
        with self.lock:
            last = self.account.get("LastSeenOnline")
            gap = (now - datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.UTC)).total_seconds() if last else None
            if gap is not None and gap < 300:
                return
            if gap is not None and gap <= self.PLAY_GAP:
                days = self.account.setdefault(self.PLAY_TIME, {})
                day = now.strftime("%Y-%m-%d")
                days[day] = days.get(day, 0) + int(gap)
                oldest = (now - datetime.timedelta(days=self.PLAY_DAYS)).strftime("%Y-%m-%d")
                for d in [d for d in days if d < oldest]:
                    del days[d]
            self.account["LastSeenOnline"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()

    # -- Gezielte Angriffe ---------------------------------------------------
    # AttackSettings: TargetedAttackMaxCount 5, TargetedAttackReloadDuration
    # 8h. Ein verbrauchter Angriff lädt per TargetedAttackExpirable nach; der
    # Client meldet den Ablauf nicht, der Server lädt bei jeder Anfrage nach.

    @functools.cached_property
    def _targeted_settings(self):
        a = _load_json(os.path.join(SETTINGS, "GeneralSettings", "AttackSettings.json"))
        return a["TargetedAttackMaxCount"], castles.duration(a["TargetedAttackReloadDuration"])

    def _reload_expirable(self):
        return next((e for e in self.account.get("Expirables") or []
                     if e.get("ExpirableType") == "TargetedAttackReload"), None)

    def use_targeted_attack(self, now):
        """Einen gezielten Angriff verbrauchen; liefert Notifications."""
        max_count, reload = self._targeted_settings
        out = []
        with self.lock:
            count = self.account.get("TargetedAttackAvailableCount", max_count)
            self.account["TargetedAttackAvailableCount"] = max(0, count - 1)
            if not self._reload_expirable():
                e = {"$type": T("TargetedAttackExpirable"),
                     "Id": f"{random.getrandbits(96):024x}",
                     "ExpirableType": "TargetedAttackReload",
                     "DueDate": (now + datetime.timedelta(seconds=reload)).strftime(
                         "%Y-%m-%dT%H:%M:%SZ")}
                self.account.setdefault("Expirables", []).append(e)
                out.append(("ExpirableAdded", {"Expirable": dict(e)}))
            self._save()
        self.log(f"    [state] gezielter Angriff: noch "
                 f"{self.account['TargetedAttackAvailableCount']}")
        return out

    def reload_targeted_attacks(self, now):
        """Fällige Nachladungen gutschreiben; liefert Notifications."""
        max_count, reload = self._targeted_settings
        out = []
        with self.lock:
            e = self._reload_expirable()
            if not e:
                return out
            parse = lambda d: datetime.datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.UTC)
            due = parse(e["DueDate"])
            count = self.account.get("TargetedAttackAvailableCount", max_count)
            while due <= now and count < max_count:
                count += 1
                due += datetime.timedelta(seconds=reload)
                out.append(("TargetedAttackReloaded", {"TargetedAttackAvailableCount": count}))
            if not out:
                return out
            self.account["TargetedAttackAvailableCount"] = count
            if count >= max_count:
                self.account["Expirables"].remove(e)
            else:
                e["DueDate"] = due.strftime("%Y-%m-%dT%H:%M:%SZ")
                out.append(("ExpirableUpdated", {"ExpirableId": e["Id"],
                                                 "DueDate": e["DueDate"]}))
            self._save()
        self.log(f"    [state] gezielte Angriffe nachgeladen: {count}")
        return out

    # -- Boosts (Verbrauchsgüter) --------------------------------------------
    # ActivateConsumableCommand startet einen Boost: ActiveConsumable plus
    # ConsumableExpirable (Laufzeit = Template-Duration). Pro ConsumableType
    # ist einer aktiv, ein neuer ersetzt den alten. Wirkung auf die Beute
    # (Faktor = Template-Feld) rechnet attack.end_attack, Magic Find wirkt
    # beim Würfeln der Kreatur-Drops in attack.start_attack.

    BOOST_FIELDS: ClassVar[dict[str, tuple[Any, Any]]] = {
        "XpBoost": ("Xp", "IncreasedXp"),
        "GoldBoost": ("Gold", "AttackIncreasedGold"),
        "LifeForceBoost": ("LifeForce", "AttackIncreasedLifeForce"),
        "MagicFindBoost": ("MagicFind", "IncreasedChance"),
        "CastleDPSBoost": (None, None)}

    @staticmethod
    def _consumable_type(template):
        """ConsumableType aus dem Template-$type (XpBoostConsumableTemplate)."""
        name = template["$type"].split(",")[0].rsplit(".", 1)[-1]
        return name.removesuffix("ConsumableTemplate")

    def _cmd_ActivateConsumableCommand(self, cmd):
        tid = cmd["ConsumableTemplateId"]
        template = self.data.consumables[tid]
        ctype = self._consumable_type(template)
        if ctype not in self.BOOST_FIELDS or not template.get("Duration"):
            raise ValueError(f"Verbrauchsgut {tid} ({ctype}) nicht aktivierbar")
        slot = next((s for s in self._slots()
                     if s["SlotIndex"] == cmd["InventorySlotIndex"]
                     and s["Item"].get("TemplateId") == tid), None)
        if slot is None:
            raise KeyError(f"Verbrauchsgut {tid} nicht in Slot {cmd['InventorySlotIndex']}")
        self._consume_one(slot)
        self._end_consumable(ctype)
        seconds = castles.duration(template["Duration"])
        now = datetime.datetime.now(datetime.UTC)
        eid = f"{random.getrandbits(96):024x}"
        expirable = {"$type": T("ConsumableExpirable"), "Id": eid,
                     "ExpirableType": "Consumable",
                     "DueDate": (now + datetime.timedelta(seconds=seconds)).strftime(
                         "%Y-%m-%dT%H:%M:%SZ"),
                     "ConsumableType": ctype, "TemplateId": tid}
        active = {"$type": T("ActiveConsumable"), "ConsumableType": ctype,
                  "ExpirableId": eid,
                  # TimeSpan liest der Client nur als "20m"/"1h"
                  # (docs/30-protokoll.md), daher die Template-Angabe
                  "InitialDuration": template["Duration"],
                  "TemplateId": tid}
        self.account.setdefault("Expirables", []).append(expirable)
        self.account.setdefault("ActiveConsumables", []).append(active)
        self.events.append(("ConsumableActivated", {"ActiveConsumable": dict(active)}))
        self.events.append(("ExpirableAdded", {"Expirable": dict(expirable)}))
        self.log(f"    [state] {ctype} {tid} aktiviert ({template['Duration']})")

    def _cmd_ActivateConsumableOnItemCommand(self, cmd):
        """Färbemittel (DyeConsumableTemplate) aus InventorySlotIndex auf
        das angelegte Teil HeroItemSlot des gewählten Helden: DyeTemplateId
        setzen, ein Stück verbrauchen."""
        slot = next((s for s in self._slots()
                     if s["SlotIndex"] == cmd["InventorySlotIndex"]), None)
        tid = slot and slot["Item"].get("TemplateId")
        if slot is None or self._consumable_type(
                self.data.consumables.get(tid, {"$type": ""})) != "Dye":
            raise KeyError(f"kein Färbemittel in Slot {cmd['InventorySlotIndex']}")
        part = cmd["HeroItemSlot"]
        part = CLIENT_SLOTS[part] if isinstance(part, int) else part
        hero_id = self.account["SelectedHeroId"]
        item = self._hero(hero_id)["Equipment"].get(part)
        if not item:
            raise KeyError(f"Held {hero_id}: {part} nicht angelegt")
        item["DyeTemplateId"] = tid
        self._consume_one(slot)
        self.events.append(("HeroDyeApplied", {
            "DyeTemplateId": tid, "HeroItemSlot": cmd["HeroItemSlot"],
            "HeroSpecContainerId": hero_id}))
        self.log(f"    [state] Held {hero_id}: {part} gefärbt (Dye {tid})")

    def _consume_one(self, slot):
        """Ein Stück aus dem Inventar-Slot verbrauchen und melden. Den
        Verbrauch bucht der Client nicht selbst (wie ConsumableManager.
        ActivateConsumable); ohne Meldung bleibt der Slot im Client belegt
        und die nächste Beute dorthin vermischt sich mit dem alten Eintrag."""
        slot["StackCount"] -= 1
        if slot["StackCount"] <= 0:
            self._take_slot(slot["SlotIndex"])
            self.events.append(("HeroInventoryRemoved", {"SlotIndex": slot["SlotIndex"]}))
        else:
            self.events.append(("HeroInventoryUpdated", {
                "SlotIndex": slot["SlotIndex"], "StackCount": slot["StackCount"]}))

    def _end_consumable(self, ctype):
        """Aktiven Boost dieses Typs samt Expirable entfernen."""
        gone = [a for a in self.account.get("ActiveConsumables") or []
                if a["ConsumableType"] == ctype]
        ids = {a["ExpirableId"] for a in gone}
        self.account["ActiveConsumables"] = [
            a for a in self.account.get("ActiveConsumables") or [] if a not in gone]
        self.account["Expirables"] = [e for e in self.account.get("Expirables") or []
                                      if e.get("Id") not in ids]
        return gone

    def expire_consumables(self, now):
        """Abgelaufene Boosts entfernen; liefert Notifications."""
        out = []
        with self.lock:
            due = {e["Id"] for e in self.account.get("Expirables") or []
                   if e.get("ExpirableType") == "Consumable"
                   and e["DueDate"] <= now.strftime("%Y-%m-%dT%H:%M:%SZ")}
            for a in list(self.account.get("ActiveConsumables") or []):
                if a["ExpirableId"] in due:
                    self._end_consumable(a["ConsumableType"])
                    out.append(("ConsumableExpired", {
                        "ConsumableType": a["ConsumableType"],
                        "TemplateId": a["TemplateId"]}))
                    self.log(f"    [state] {a['ConsumableType']} abgelaufen")
            if out:
                self._save()
        return out

    def expire_buyback(self, now):
        """Fällige Rückkauf-Einträge entfernen (BUYBACK_DUE; ältere Einträge
        ohne Eintrag dort: CreationDate + BuyBackTimeout). Nur beim Laden des
        Accounts, der den neuen Stand trägt; daher keine Notifications."""
        timeout = datetime.timedelta(seconds=castles.duration(self.data.shop["BuyBackTimeout"]))
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            items = (self.account.get("BuyBack") or {}).get("BuyBackItems")
            if not items:
                return
            due = self.account.get(self.BUYBACK_DUE) or {}
            def due_of(x):
                return due.get(x["Id"]) or (datetime.datetime.strptime(
                    x["CreationDate"], "%Y-%m-%dT%H:%M:%SZ") + timeout).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            gone = [x for x in items if due_of(x) <= stamp]
            if not gone:
                return
            items[:] = [x for x in items if x not in gone]
            for x in gone:
                due.pop(x["Id"], None)
            self.log(f"    [state] Rückkauf: {len(gone)} Einträge abgelaufen")
            self._save()

    # -- Burgschild ------------------------------------------------------------
    # Nach dem Plündern der Burg (attack.shield_duration): ShieldEndExpirable,
    # verwiesen von BuildInfo.ShieldEndExpirableId. Der Client meldet den
    # Ablauf nicht, der Server prüft bei jeder Anfrage (expire_shield).
    # Meldungen: ExpirableAdded + ShieldAdded, beim Ablauf/Entfernen
    # ShieldExpired (entfernt auch das Expirable im Client; ein
    # ExpirableUpdated danach: Assert "Cannot update expirable id").

    def add_shield(self, seconds, now):
        """Schild für seconds Sekunden anlegen (ersetzt einen laufenden);
        liefert Notifications."""
        with self.lock:
            out = [("ShieldExpired", {"ExpirableId": x["Id"]})
                   for x in self._drop_shield_expirables()]
            e = {"$type": T("ShieldEndExpirable"), "Id": f"{random.getrandbits(96):024x}",
                 "ExpirableType": "ShieldEnd",
                 "DueDate": (now + datetime.timedelta(seconds=seconds)).strftime(
                     "%Y-%m-%dT%H:%M:%SZ")}
            self.account.setdefault("Expirables", []).append(e)
            self.account["BuildInfo"]["ShieldEndExpirableId"] = e["Id"]
            self._save()
        self.log(f"    [state] Schild bis {e['DueDate']}")
        return out + [("ExpirableAdded", {"Expirable": dict(e)}),
                      ("ShieldAdded", {"ExpirableId": e["Id"]})]

    def _drop_shield_expirables(self, keep=None):
        """ShieldEnd-Expirables außer `keep` entfernen; liefert die entfernten."""
        items = self.account.get("Expirables") or []
        gone = [x for x in items if x.get("ExpirableType") == "ShieldEnd" and x["Id"] != keep]
        items[:] = [x for x in items if x not in gone]
        return gone

    def _drop_orphan_shields(self):
        """Alte Accounts: ShieldEnd-Expirables, auf die BuildInfo nicht mehr
        verweist (früher ließ ein neuer Schild den alten liegen; der Client
        meldete ihn dann bei jedem Login per ExpireExpirableCommand)."""
        return bool(self._drop_shield_expirables(
            keep=self.account["BuildInfo"].get("ShieldEndExpirableId")))

    def expire_shield(self, now, remove=False):
        """Abgelaufenen (mit remove: jeden) Schild entfernen; liefert
        Notifications."""
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            build = self.account["BuildInfo"]
            eid = build.get("ShieldEndExpirableId") or NO_EXPIRABLE
            e = next((x for x in self.account.get("Expirables") or []
                      if x["Id"] == eid), None)
            if eid == NO_EXPIRABLE or not (remove or e is None or e["DueDate"] <= stamp):
                return []
            if e is not None:
                self.account["Expirables"].remove(e)
            build["ShieldEndExpirableId"] = NO_EXPIRABLE
            self._save()
        self.log("    [state] Schild " + ("entfernt" if remove else "abgelaufen"))
        return [("ShieldExpired", {"ExpirableId": eid})]

    # -- Helden zum Probespielen (free_trials.py) -----------------------------

    # Ids der Trial-Events, deren Helden der Spieler schon bekommen hat; nur
    # serverseitig, GetAccountInformation entfernt das Feld
    FREE_TRIALS = "FreeTrialEvents"
    # abgelaufene, nicht gekaufte Probe-Helden (nur serverseitig): ein
    # späterer Kauf holt sie mit Level und Ausrüstung zurück
    EXPIRED_TRIALS = "ExpiredTrialHeroes"

    def expire_trials(self, now):
        """Probe-Helden nach Hero.ExpirationDate aus dem Account nehmen. Der
        finale Client wertet das Datum nicht aus und kennt keine Meldung zum
        Entfernen eines Helden; daher nur beim Laden des Accounts
        (GetAccountInformation), danach kennt der Client ihn nicht mehr."""
        stamp = free_trials.iso(now)
        with self.lock:
            gone = [h for h in self.account["Heroes"]
                    if h.get("ExpirationDate") and h["ExpirationDate"] <= stamp]
            if not gone:
                return
            self.account["Heroes"] = [h for h in self.account["Heroes"] if h not in gone]
            self.account.setdefault(self.EXPIRED_TRIALS, []).extend(gone)
            ids = [h["HeroSpecContainerId"] for h in gone]
            if self.account.get("SelectedHeroId") in ids and self.account["Heroes"]:
                self.account["SelectedHeroId"] = self.account["Heroes"][0]["HeroSpecContainerId"]
            self.log(f"    [state] Trial abgelaufen: Helden {ids} entfernt")
            self._save()

    def free_trials(self, now):
        """Helden des laufenden Trials freischalten (einmal je Event, nur
        nicht besessene) und erreichte Rabatt-Ziele der Probe-Helden
        eintragen; liefert Notifications."""
        event = free_trials.active(now)
        out = []
        with self.lock:
            heroes = {h["HeroSpecContainerId"]: h for h in self.account["Heroes"]}
            granted = self.account.setdefault(self.FREE_TRIALS, [])
            if event and event["Id"] not in granted:
                granted.append(event["Id"])
                if event["Hero"] not in heroes:
                    hero = self._new_hero(event["Hero"])
                    hero["ExpirationDate"] = free_trials.iso(event["End"])
                    hero["PriceReductionIds"] = []
                    self.account["Heroes"].append(hero)
                    heroes[event["Hero"]] = hero
                    self.spells_unlocked += self._unlock_spells()
                    out.append(("HeroUnlocked", {"Hero": copy.deepcopy(hero),
                                                 "HeroSpecContainerId": event["Hero"]}))
                    out.append(("FreeTrialActivated", {
                        "FreeTrialType": "HeroUnlock", "HeroSpecContainerId": event["Hero"]}))
                    self.log(f"    [state] Trial {event['Id']}: Held {event['Hero']} "
                             f"bis {hero['ExpirationDate']}")
            conditions = free_trials.conditions()
            for hero in heroes.values():
                if hero.get("ExpirationDate") is None:
                    continue  # kein Probe-Held
                done = hero.setdefault("PriceReductionIds", [])
                for cid in free_trials.CONDITION_IDS:
                    if cid not in done and free_trials.reached(hero, conditions[cid]):
                        done.append(cid)
                        out.append(("FreeTrialConditionCompleted", {
                            "HeroSpecContainerId": hero["HeroSpecContainerId"],
                            "PriceReductionConditionCompletedId": cid}))
                        self.log(f"    [state] Trial-Ziel {cid} für Held "
                                 f"{hero['HeroSpecContainerId']}")
            if out:
                self._save()
        return out

    # -- Delegierte Validierung ------------------------------------------------
    # Validation Ticket (SKU ItemType 22, ItemId = Angriffsregion, 10 Blings;
    # Clan-Ticket ohne Preis): die Burg im Stand beim Kauf geht an andere
    # Spieler (castles.validation_castle); der erste Sieg validiert sie,
    # der Validierer bekommt DelegatedCastleValidationReward. Bis dahin
    # CastleValidationDelegationExpirable (zeigt "Pending Castle
    # Validation"); beim Ablauf zurück erstattet. Dauer laut Daten unbekannt.
    VALIDATION_DELEGATION = "ValidationDelegation"
    DELEGATION_DURATION = datetime.timedelta(days=1)

    def _buy_validation_delegation(self, sku):
        if self.account.get(self.VALIDATION_DELEGATION):
            raise ValueError("Validierung schon delegiert")
        price = sku.get("Price") or {}
        if price:
            field = CURRENCY[price["CurrencyType"]]
            if self.account["Wallet"][field] < price["Amount"]:
                raise ValueError(f"zu wenig {field}")
            self.account["Wallet"][field] -= price["Amount"]
        now = datetime.datetime.now(datetime.UTC)
        e = {"$type": T("CastleValidationDelegationExpirable"),
             "Id": f"{random.getrandbits(96):024x}",
             "ExpirableType": "CastleValidationDelegation",
             "DueDate": (now + self.DELEGATION_DURATION).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.account.setdefault("Expirables", []).append(e)
        build = self.account["BuildInfo"]
        self.account[self.VALIDATION_DELEGATION] = {
            "ExpirableId": e["Id"], "Region": sku["ItemId"], "Guild": not price,
            "Price": price.get("Amount", 0),
            "Rooms": copy.deepcopy(build["Draft"]["Rooms"]),
            "MineStatuses": copy.deepcopy(build.get("MineStatuses") or {})}
        self.events.append(("ExpirableAdded", {"Expirable": dict(e)}))
        self.log(f"    [state] Validierung delegiert (Region {sku['ItemId']}, "
                 f"{'Clan' if not price else '-' + str(int(price['Amount']))}) bis {e['DueDate']}")

    def delegation(self):
        """Offenes Validierungs-Ticket (Kopie) oder None."""
        with self.lock:
            return copy.deepcopy(self.account.get(self.VALIDATION_DELEGATION))

    def _end_delegation(self):
        """Ticket und Expirable entfernen; liefert das Ticket."""
        d = self.account.pop(self.VALIDATION_DELEGATION)
        self.account["Expirables"] = [x for x in self.account.get("Expirables") or []
                                      if x["Id"] != d["ExpirableId"]]
        return d

    def complete_delegation(self, seconds):
        """Ein anderer Spieler hat die Burg validiert: der Stand beim Kauf
        wird veröffentlicht wie bei validate_draft. Liefert Notifications
        (leer, wenn das Ticket nicht mehr offen ist)."""
        with self.lock:
            if not self.account.get(self.VALIDATION_DELEGATION):
                return []
            d = self._end_delegation()
            build = self.account["BuildInfo"]
            # seit dem Kauf geänderter Entwurf bleibt unveröffentlicht
            published = build["Draft"]["Rooms"] == d["Rooms"]
            build["IsDraftPublished"] = published
            build["IsRollbackAvailable"] = True
            self.account[self.VALID_BLUEPRINT] = copy.deepcopy(
                {"Rooms": d["Rooms"], "MineStatuses": d["MineStatuses"]})
            self.account[self.VALIDATION_DURATION] = seconds
            self.account[self.PUBLISHED_ROOMS] = copy.deepcopy(d["Rooms"])
            self._save()
        self.log(f"    [state] Burg durch Delegation validiert ({seconds:.1f} s)")
        return [("ExpirableRemoved", {"ExpirableId": d["ExpirableId"]}),
                ("DraftValidated", {"CastleValidationDuration": seconds,
                                    "DefenderAccountId": self.account["AccountId"],
                                    "IsDraftPublished": published,
                                    "IsRollbackAvailable": True})]

    def _cancel_delegation(self):
        """Ticket beenden und den Preis erstatten; liefert Notifications."""
        if not self.account.get(self.VALIDATION_DELEGATION):
            return []
        d = self._end_delegation()
        refund = self._credit("PremiumCash", d["Price"]) if d["Price"] else 0
        self.log(f"    [state] Validierungs-Ticket beendet, +{refund} PremiumCash")
        out = [("ExpirableRemoved", {"ExpirableId": d["ExpirableId"]})]
        if refund:
            out.append(("WalletUpdated", {"UpdateTag": 0, "Amounts": [{
                "$type": T("CurrencyAmount"), "Amount": refund, "CurrencyType": 1}]}))
        return out

    def expire_delegation(self, now):
        """Abgelaufenes Ticket entfernen und den Preis erstatten; liefert
        Notifications."""
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            d = self.account.get(self.VALIDATION_DELEGATION)
            e = d and next((x for x in self.account.get("Expirables") or []
                            if x["Id"] == d["ExpirableId"]), None)
            if not d or (e is not None and e["DueDate"] > stamp):
                return []
            out = self._cancel_delegation()
            self._save()
        return out

    # -- Schmiede -------------------------------------------------------------
    # Inventory.ForgedItem: null = leer; sonst {ExpirableId, ForgeMode,
    # Item, ItemLevelAverage} mit ForgeExpirable bis zum Fertigstellen.

    FORGE_MODES: ClassVar[dict[int, tuple[str, str]]] = {
        16: ("Crafted", "CraftTable"), 17: ("Reforged", "ReforgeTable"),
        24: ("Upgraded", "UpgradeTable")}

    def _cmd_ForgeCraftCommand(self, cmd):
        self._forge(cmd, 16)

    def _cmd_ForgeReforgeCommand(self, cmd):
        self._forge(cmd, 17)

    def _cmd_ForgeUpgradeCommand(self, cmd):
        """Aufwerten: wie Umschmieden (InventorySlotIndex/HeroItemSlot), aber
        dasselbe Teil +1 Level; dazu Handwerksmaterial aus
        ConsumedHeroInventory [{SlotIndex, TemplateId, Count (negativ)}]."""
        self._forge(cmd, 24)

    def _forge(self, cmd, item_type):
        """Gegenstände aus InventorySlotIndexes einschmelzen, SKU (ItemType
        16/17, ItemCount = Item-Level, z. B. 13990 "Magic Forge Level 4")
        bezahlen und ein Item der Qualität cmd.Quality für den gewählten
        Helden schmieden; Level + Zuschlag nach LevelToChanceModifiers."""
        inv = self.account["Inventory"]
        if inv.get("ForgedItem"):
            raise ValueError("Schmiede belegt")
        mode, table = self.FORGE_MODES[item_type]
        sku = self.data.skus[cmd["ShopSkuCode"]]
        if sku["ItemType"] != item_type:
            raise ValueError(f"SKU {cmd['ShopSkuCode']} ist keine {mode}-SKU")
        # Herstellen: InventorySlotIndexes (+ HeroItemSlots); Umschmieden:
        # InventorySlotIndex + HeroItemSlot (0 = aus dem Inventar)
        slots = (cmd["InventorySlotIndexes"] if "InventorySlotIndexes" in cmd
                 else [cmd["InventorySlotIndex"]])
        slots = [i for i in slots if i >= 0]  # -1 = kein Inventar-Gegenstand
        worn = cmd.get("HeroItemSlots") or ([cmd["HeroItemSlot"]]
                                            if cmd.get("HeroItemSlot") else [])
        hero_equipment = self._hero(self.account["SelectedHeroId"]).get("Equipment") or {}
        worn = [(i, CLIENT_SLOTS[i]) for i in worn]  # (Client-Nummer, Slot)
        source = (hero_equipment.get(worn[0][1]) if worn else next(
            (x["Item"] for x in self._slots() if slots and x["SlotIndex"] == slots[0]), None))
        # Aufwerten schickt keine Quality: die des Ausgangsteils
        quality = cmd.get("Quality")
        if quality is None:
            quality = item_drops.item_quality(source) if source else "Magic"
        if isinstance(quality, int):
            quality = item_drops.QUALITIES[quality]
        settings = self.data.forge
        tier = settings[table]["TierTable"][quality]
        if len(slots) + len(worn) != tier["RequiredItemCount"]:
            raise ValueError(f"{mode} {quality}: {len(slots) + len(worn)} statt "
                             f"{tier['RequiredItemCount']} Gegenstände")
        if any(not hero_equipment.get(w) for _, w in worn):
            raise KeyError(f"Ausrüstung {worn} nicht alle belegt")
        if any(not any(x["SlotIndex"] == i for x in self._slots()) for i in slots):
            raise KeyError(f"Inventar-Slots {slots} nicht alle belegt")
        price = sku["Price"]
        field = CURRENCY[price["CurrencyType"]]
        wallet = self.account["Wallet"]
        if wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        mods = tier["LevelToChanceModifiers"]
        bonus = int(item_drops._weighted([(k, v) for k, v in mods.items()]) or 0)
        level = min(sku["ItemCount"] + bonus, settings["MaxForgeItemLevel"])
        hero = self.account.get("SelectedHeroId")
        if mode == "Reforged":  # gleicher Typ wie das Ausgangsteil
            item = item_drops.reforge_item(source, level, quality)
        elif mode == "Upgraded":  # dasselbe Teil, Level + Zuschlag
            if source is None:
                raise ValueError("Aufwerten ohne Ausgangsteil")
            level = min(source["ItemLevel"] + bonus, settings["MaxForgeItemLevel"])
            item = item_drops.upgrade_item(source, level, settings["PropertyLevelUpChances"])
        else:
            classes = tier.get("ItemClassTable") or {}
            if random.random() >= classes.get("HeroPlayedByUser", 1):
                hero = random.choice(classes["LootableHeroSpecIds"])
            item = item_drops.forge_item(hero, level, quality)
        if item is None:
            raise ValueError(f"kein {quality}-Item auf Level {level} für Held {hero}")
        self._consume_inventory(cmd.get("ConsumedHeroInventory"))  # Handwerksmaterial
        wallet[field] -= price["Amount"]
        if mode != "Upgraded":  # beim Aufwerten bucht der Client selbst ab
            self._wallet_event(price["CurrencyType"], -price["Amount"])
        for i in slots:
            self._take_slot(i)
            # der Client nimmt eingeschmolzene Gegenstände nicht selbst raus
            self.events.append(("HeroInventoryRemoved", {"SlotIndex": i}))
        for number, w in worn:
            if w == "MainHand":
                # ohne Waffe asserten VisualHeroHandler/InventoryHelper: sie
                # bleibt bis zum Abholen angelegt und wird dann ersetzt
                self.account[self.FORGE_MAIN_HAND] = {
                    "Hero": hero, "Old": copy.deepcopy(hero_equipment[w])}
                continue
            # getragene Teile werden verbraucht; der Client legt sie nicht
            # selbst ab, das Ergebnis kommt beim Abholen in die Taschen
            hero_equipment[w] = None
            self.events.append(("HeroEquipmentUnequip", {"HeroItemSlot": number,
                                                         "HeroSpecContainerId": hero}))

        now = datetime.datetime.now(datetime.UTC)
        eid = f"{random.getrandbits(96):024x}"
        expirable = {"$type": T("ForgeExpirable"), "Id": eid, "ExpirableType": "Forge",
                     "ForgeMode": mode,
                     "DueDate": (now + datetime.timedelta(
                         seconds=castles.duration(tier["Duration"]))).strftime(
                             "%Y-%m-%dT%H:%M:%SZ")}
        forged = {"$type": T("AccountForgedItem"), "ExpirableId": eid,
                  "ForgeMode": mode, "Item": item, "ItemLevelAverage": sku["ItemCount"]}
        inv["ForgedItem"] = forged
        self.account.setdefault("Expirables", []).append(expirable)
        # erst das Expirable: OnForgeStarted sucht es sofort
        self.events.append(("ExpirableAdded", {"Expirable": dict(expirable)}))
        self.events.append(("ForgeStarted", {"AccountForgedItem": copy.deepcopy(forged)}))
        self.log(f"    [state] Schmiede: {mode} {quality} Level {level} "
                 f"(Template {item['TemplateId']}), -{price['Amount']} {field}, "
                 f"fertig {expirable['DueDate']}")

    def _cmd_ExpireExpirableCommand(self, cmd):
        """Der Client meldet ein abgelaufenes Expirable selbst (Timer-Ende,
        mit PayToForceExpiration "Sofort fertig" zum Preis der Restzeit nach
        TimeSettings.TimeCostTable). Bisher nur die Schmiede: Expirable weg,
        ForgeItemReady. Material-O-Matic: Periode abrechnen
        (advance_material_mines)."""
        if any(e["Id"] == cmd["ExpirableId"] and e.get("ExpirableType") == "MineProduction"
               for e in self.account.get("Expirables") or []):
            self.events += self.advance_material_mines()
            return
        forged = self.account["Inventory"].get("ForgedItem")
        if not forged or forged["ExpirableId"] != cmd["ExpirableId"]:
            raise KeyError(f"Expirable {cmd['ExpirableId']} nicht behandelt")
        if cmd.get("PayToForceExpiration"):
            e = next(x for x in self.account["Expirables"] if x["Id"] == cmd["ExpirableId"])
            self._pay_acceleration(e["DueDate"])
        # das Expirable bleibt bis zum Abholen (ForgedItem verweist darauf;
        # nach einem Account-Laden meldet der Client es erneut als fällig)
        # ohne ForgeItemReady bleibt der Timer bei 0 stehen
        self.events.append(("ForgeItemReady", {"AccountForgedItem": copy.deepcopy(forged),
                                               "ForgeMode": forged["ForgeMode"]}))
        self.log("    [state] Schmiede: Item fertig")

    def _wallet_event(self, currency_type, delta):
        """WalletUpdated (Differenz); die Schmiede-Kosten bucht der Client
        nicht selbst ab."""
        self.events.append(("WalletUpdated", {"UpdateTag": 0, "Amounts": [{
            "$type": T("CurrencyAmount"), "Amount": delta,
            "CurrencyType": currency_type}]}))

    def _pay_acceleration(self, due_date):
        """Sofort-Fertigstellung: Preis der höchsten Schwelle <= Restzeit
        (TimeController::CalculateAcceleratorPrice)."""
        due = datetime.datetime.strptime(due_date, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.UTC)
        left = (due - datetime.datetime.now(datetime.UTC)).total_seconds()
        if left <= 0:
            return
        cost = [r["Cost"] for r in self.data.time["TimeCostTable"]
                if castles.duration(r["TimeThreshold"]) <= left][-1]
        field = CURRENCY[cost["CurrencyType"]]
        if self.account["Wallet"][field] < cost["Amount"]:
            raise ValueError(f"zu wenig {field}")
        self.account["Wallet"][field] -= cost["Amount"]
        self._wallet_event(cost["CurrencyType"], -cost["Amount"])
        self.log(f"    [state] Sofort fertig ({int(left)} s übrig): -{cost['Amount']} {field}")

    def _cmd_ForgeCollectCommand(self, cmd):
        """Fertiges Item dorthin, wo der Client es ablegt."""
        inv = self.account["Inventory"]
        forged = inv.get("ForgedItem")
        if not forged:
            raise ValueError("Schmiede leer")
        main_hand = self.account.get(self.FORGE_MAIN_HAND)
        if main_hand:
            # Umgeschmiedete Waffe: der Client legt sie wie jedes Ergebnis in
            # die Taschen und behält die alte in der Hand (Equip-Meldungen
            # wendet er hier nicht an) und schickt im selben Paket oft weitere
            # Befehle auf dieser Sicht (alte ablegen/verkaufen, neue anlegen).
            # Daher erst wie der Client ablegen, am Ende des Pakets die alte
            # entfernen (_settle_forged_weapon) und mit ForgeItemCollected
            # (fehlt im Client -> Account neu laden) die Anzeige korrigieren.
            main_hand["New"] = copy.deepcopy(forged["Item"])
            self.events.append(("ForgeItemCollected", {}))
        # der Client wählt das Ziel: InventorySlotIndex oder HeroItemSlot
        if cmd.get("HeroItemSlot"):
            slot = CLIENT_SLOTS[cmd["HeroItemSlot"]]
            self._hero(self.account["SelectedHeroId"])["Equipment"][slot] = forged["Item"]
            index = slot
        else:
            index = cmd.get("InventorySlotIndex", -1)
            if index < 0:
                index = self._free_slot()
            if index is None:
                raise ValueError("Inventar voll")
            self._put_slot(index, forged["Item"])
        self.account["Expirables"] = [x for x in self.account.get("Expirables") or []
                                      if x["Id"] != forged["ExpirableId"]]
        inv["ForgedItem"] = None
        # Für andere Teile kein ForgeItemCollected (fehlt im Client -> Neuladen);
        # das Abholen in Taschen/Slots wendet der Client selbst an.
        self.log(f"    [state] Schmiede: Template {forged['Item']['TemplateId']} -> Slot {index}")

    def _settle_forged_weapon(self):
        """Nach einem Paket mit dem Abholen einer umgeschmiedeten Waffe: die
        alte (verbraucht) entfernen, wo immer sie jetzt liegt; ist sie noch
        angelegt, kommt die neue in die Hand. Verkaufte Teile verschwinden
        samt Erlös aus dem Rückkauf."""
        pending = self.account.get(self.FORGE_MAIN_HAND)
        if not pending or "New" not in pending:
            return
        del self.account[self.FORGE_MAIN_HAND]
        equipment = self._hero(pending["Hero"])["Equipment"]
        if equipment.get("MainHand") == pending["Old"]:
            equipment["MainHand"] = None
        elif not self._remove_item(pending["Old"]):
            self.log("    [state] Schmiede: alte Waffe nicht gefunden")
        # ohne Waffe asserted der Client -> die neue anlegen
        if equipment.get("MainHand") is None and self._remove_item(pending["New"]):
            equipment["MainHand"] = pending["New"]
        self.log("    [state] Schmiede: alte Waffe entfernt, Hand: Template "
                 f"{(equipment.get('MainHand') or {}).get('TemplateId')}")

    def _remove_item(self, item):
        """Gegenstand aus den Taschen oder dem Rückkauf (Erlös zurück)
        entfernen; True, wenn gefunden."""
        for slot in self._slots():
            if slot["Item"] == item:
                self._take_slot(slot["SlotIndex"])
                return True
        items = (self.account.get("BuyBack") or {}).get("BuyBackItems") or []
        for entry in items:
            if entry.get("Item") == item:
                items.remove(entry)
                (self.account.get(self.BUYBACK_DUE) or {}).pop(entry["Id"], None)
                price = entry["Price"]
                self.account["Wallet"][CURRENCY[price["CurrencyType"]]] -= price["Amount"]
                self._wallet_event(price["CurrencyType"], -price["Amount"])
                return True
        return False

    def attack_boosts(self):
        """(aktive Boosts, {"Xp"/"Gold"/"LifeForce": Zusatzfaktor})."""
        with self.lock:
            active = copy.deepcopy(self.account.get("ActiveConsumables") or [])
        factors = {}
        for a in active:
            key, field = self.BOOST_FIELDS.get(a["ConsumableType"], (None, None))
            if key:
                factors[key] = self.data.consumables[a["TemplateId"]].get(field, 0)
        return active, factors

    def set_theme(self, theme_id):
        """Burg-Theme wählen (ThemeService.SetCurrentTheme): nur
        freigeschaltete aus BuildInfo.InventoryThemes; True bei Erfolg."""
        with self.lock:
            bi = self.account["BuildInfo"]
            if theme_id not in (bi.get("InventoryThemes") or []):
                return False
            bi["Draft"]["ThemeId"] = theme_id
            self._save()
        self.log(f"    [state] Burg-Theme {theme_id}")
        return True

    def castle_rating(self, castle_id):
        return (self.account.get(self.RATINGS) or {}).get(str(castle_id), 0)

    # Achievement-Blings (balance.py) je Id ausgezahlt {Id: Blings}; nur
    # serverseitig, GetAccountInformation entfernt das Feld
    ACHIEVEMENT_BLINGS = "AchievementBlingsPaid"

    def _check_achievements(self):
        done = achievements.complete(self.account, profiles.achievement_specs())
        # Blings zuerst: mit WalletUpdated hinter AchievementCompleted kam das
        # Popup im Spiel nicht mehr
        self._pay_achievement_blings()
        for aid, points in done:
            self.events.append(("AchievementCompleted", {
                "AchievementId": aid, "GamerScoreToAdd": points,
                "HasUiNotification": True}))
            self.log(f"    [state] Errungenschaft {aid} erreicht (+{points})")

    def _pay_achievement_blings(self):
        """Blings je abgeschlossenem Achievement (einmal, zum dann gültigen
        Betrag); auch für vorher erreichte, sobald die Bling-Wirtschaft an
        ist. Der Client kennt keine Achievement-Belohnung: das Popup zeigt
        den Betrag per UI-Patch, gebucht wird mit WalletUpdated."""
        if not balance.enabled():
            return
        paid = self.account.setdefault(self.ACHIEVEMENT_BLINGS, {})
        total = 0
        for aid in self.account.get("CompletedAchievements") or []:
            if str(aid) in paid:
                continue
            paid[str(aid)] = balance.achievement_blings(aid)
            got = self._credit("PremiumCash", paid[str(aid)])
            self._tag("achievements", got)
            total += got
        if total:
            self.events.append(("WalletUpdated", {"UpdateTag": 0, "Amounts": [{
                "$type": T("CurrencyAmount"), "Amount": total, "CurrencyType": 1}]}))
            self._book("achievements")  # Kontobuch: eigene Quelle
            self.log(f"    [state] Achievement-Blings +{total}")

    def complete_castle_objectives(self, gains):
        """Schließt die Burg-Objectives ab, die der Angriff erfüllt, bucht
        ihre Belohnungen und schaltet Folge-Objectives frei.

        Liefert {"completed": [Ids], "unlocked": [AccountObjective],
        "wallet": {Feld: Delta}, "inventory": [(Meldung, Felder)], "hero", "xp",
        "level_changed"}.
        """
        with self.lock:
            done = objectives.completed_by_attack(
                self.account, gains.get("castle_id"),
                gains.get("castle_type", 1), gains.get("completion_type"),
                gains.get("destroyed") or {})
            out = self._complete_objectives(done, gains.get("hero"))
            out["inventory"] += self._take_overflow()
            return out

    def complete_boss_region(self, gains):
        """Bosssieg schließt die Region ab und schaltet die nächste frei
        (Originalserver AttackSelectionManager.OnAttackEnded /
        CompleteAndUnlockAttackRegion: Region mit BossCastleId = Burg und
        Status 2 -> 3, Region Id+1 bis zur letzten -> 2). Der Client führt
        die Regionen je Held, daher beim angreifenden Helden und am Account.
        Schaltet die Boss-Quest der neuen Region frei.
        Liefert (abgeschlossen, freigeschaltet oder None, neue Objectives)
        oder None."""
        if gains.get("completion_type") != 0 or gains.get("practice"):
            return None
        path = os.path.join(loot.SETTINGS, "GeneralSettings", "AttackAdvisorSettings.json")
        regions = _load_json(path)["AttackRegions"]
        boss = next((r["AttackRegionId"] for r in regions
                     if r.get("BossCastleId") == gains.get("castle_id")), None)
        if boss is None:
            return None
        nxt = boss + 1 if boss < max(r["AttackRegionId"] for r in regions) else None
        with self.lock:
            hero = next((h for h in self.account["Heroes"]
                         if h["HeroSpecContainerId"] == gains.get("hero")), None)
            if hero is None or not self._advance_region(hero, boss, nxt):
                return None
            self._advance_region(self.account, boss, nxt)
            unlocked = self._unlock_objectives(objectives.newly_unlocked(
                self.account, regions=() if nxt is None else (nxt,)))
            self._save()
            return boss, nxt, unlocked

    def unlock_region_objectives(self):
        """Beim Login: Boss-Quests aller freigeschalteten Regionen nachtragen
        (Spielstände, deren Region schon vor der Freischaltung per Bosssieg
        offen war)."""
        with self.lock:
            regions = {r["AttackRegionId"] for h in self.account.get("Heroes", [])
                       for r in h.get("AttackRegions") or [] if r.get("Status", 0) >= 2}
            if self._unlock_objectives(objectives.newly_unlocked(self.account,
                                                                 regions=regions)):
                self._save()

    @staticmethod
    def _advance_region(owner, boss, nxt):
        regions = owner.setdefault("AttackRegions", [])
        by_id = {r["AttackRegionId"]: r for r in regions}
        if by_id.get(boss, {}).get("Status") != 2:
            return False
        by_id[boss]["Status"] = 3
        if nxt is not None:
            if nxt in by_id:
                by_id[nxt]["Status"] = max(by_id[nxt]["Status"], 2)
            else:
                regions.append({"$type": T("AccountAttackRegion"),
                                "AttackRegionId": nxt, "Status": 2})
        return True

    def complete_client_objective(self, objective_id):
        """ObjectiveService.CompleteClientObjective: Objectives vom Typ
        ClientObjective (z. B. 307 "Chests filling", 2000 Gold + 2000
        Lebenskraft im Bau-Tutorial) prüft der Client selbst; der Server
        bucht Belohnung und Folge-Objectives. Ergebnis wie
        complete_castle_objectives."""
        with self.lock:
            obj = objectives.definitions().get(objective_id)
            status = {o["ObjectiveId"]: o["Status"] for o in self.account["Objectives"]}
            done = ([objective_id] if obj
                    and obj["$type"].split(",")[0].endswith(".ClientObjective")
                    and status.get(objective_id) != objectives.COMPLETED else [])
            if done and objective_id not in status:
                self.account["Objectives"].append({
                    "$type": T("AccountObjective"), "HasViewed": False,
                    "LastStatusDate": "", "ObjectiveId": objective_id,
                    "ProgressionCount": 0, "Status": objectives.UNLOCKED})
            out = self._complete_objectives(
                done, self.account.get("SelectedHeroId"))
            out["inventory"] += self._take_overflow()
            return out

    def _complete_objectives(self, done, hero_id):
        """Objectives `done` abschließen (Lock gehalten)."""
        out = {"completed": done, "unlocked": [], "wallet": {}, "inventory": [],
               "hero": None, "xp": 0, "level_changed": False}
        if not done:
            return out
        now = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        by_id = {o["ObjectiveId"]: o for o in self.account["Objectives"]}
        items = []
        for oid in done:
            by_id[oid]["Status"] = objectives.COMPLETED
            by_id[oid]["LastStatusDate"] = now
            currency, xp, reward_items = objectives.rewards(oid)
            for field, amount in currency.items():
                got = self._credit(field, amount)
                if field == "PremiumCash":
                    self._tag("quests", got)
                out["wallet"][field] = out["wallet"].get(field, 0) + got
            out["xp"] += xp
            items += reward_items
            self.log(f"    [state] Objective {oid} abgeschlossen: "
                     f"{currency} {xp} XP, {len(reward_items)} Items")
        # je Slot nur der Endstand (Material stapelt auf denselben Slot;
        # mehrere HeroInventoryAdded für einen Slot ergäben falsche Mengen)
        occupied = self._occupied()
        out["inventory"] = self._inventory_events(list(
            {x["SlotIndex"]: x for x in self._add_reward_items(items)}.values()),
            occupied)
        if out["xp"] and hero_id is not None:
            out["hero"], out["level_changed"] = self._add_xp(hero_id, out["xp"])
        out["unlocked"] = self._unlock_objectives(objectives.newly_unlocked(self.account, done))
        self._save()
        return out

    def _unlock_objectives(self, ids):
        """Objectives als freigeschaltet eintragen; Einträge für ObjectiveUnlocked."""
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for oid in ids:
            entry = {"$type": T("AccountObjective"), "HasViewed": False,
                     "LastStatusDate": now, "ObjectiveId": oid,
                     "ProgressionCount": 0, "Status": objectives.UNLOCKED}
            self.account["Objectives"].append(entry)
            out.append(copy.deepcopy(entry))
            self.log(f"    [state] Objective {oid} freigeschaltet")
        return out

    def _add_xp(self, hero_id, xp):
        hero = self._hero(hero_id)
        hero["XP"] += xp
        level = max(hero["Level"], loot.level_for_xp(hero["XP"]))
        level_changed = level != hero["Level"]
        hero["Level"] = level
        if level_changed:
            self.spells_unlocked += self._unlock_spells()
        return copy.deepcopy(hero), level_changed

    def _occupied(self):
        return {s["SlotIndex"] for s in self._slots()}

    @staticmethod
    def _inventory_events(slots, occupied):
        """Meldungen zu geänderten Slots: neuer Slot -> HeroInventoryAdded,
        gewachsener Stapel -> HeroInventoryUpdated (Added auf einen belegten
        Slot quittiert der Client mit "Slot is already used")."""
        return [("HeroInventoryAdded", {"InventorySlot": s, "NewlyAdded": True})
                if s["SlotIndex"] not in occupied else
                ("HeroInventoryUpdated", {"SlotIndex": s["SlotIndex"],
                                          "StackCount": s["StackCount"]})
                for s in slots]

    def _add_reward_items(self, items, keep_overflow=True):
        """Belohnungen aus objectives.rewards: Material stapelt auf einen
        vorhandenen Slot derselben TemplateId, sonst neuer Slot."""
        added = []
        for kind, template_id, n in items:
            if kind == "equipment":  # n = ItemLevel der Belohnung
                added += self._add_items([self._item_from_template(template_id, n)],
                                         keep_overflow)
                continue
            slot = next((s for s in self._slots()
                         if s["Item"].get("TemplateId") == template_id
                         and s["Item"]["$type"] == T("HeroConsumableItem")), None)
            if slot is not None:
                slot["StackCount"] += n
                added.append(copy.deepcopy(slot))
                self.log(f"    [state] Material {template_id} +{n} -> Slot "
                         f"{slot['SlotIndex']}")
                continue
            index = self._free_slot()
            if index is None:
                self._overflow({"$type": T("HeroConsumableItem"),
                                "TemplateId": template_id}, n, keep_overflow)
                continue
            self._put_slot(index, {"$type": T("HeroConsumableItem"),
                                   "TemplateId": template_id}, n)
            added.append(copy.deepcopy(self._slots()[-1]))
            self.log(f"    [state] Material {template_id} x{n} -> Slot {index}")
        # je Slot nur der letzte Stand (gestapeltes Material)
        return list({s["SlotIndex"]: s for s in added}.values())

    def _use_consumables(self, hero_id, abilities):
        """Im Angriff getrunkene Tränke abziehen. EndAttack meldet je Einsatz
        nur die AbilityId (ConsumableUsed, z. B. 20 für alle Heiltränke);
        verbraucht wird der erste Gürtel-Trank mit dieser Ability und Vorrat.
        Der Gürtel verweist auf die Inventarstapel, beides wird gekürzt.
        Liefert die Meldungen zu den Stapeln (wie ConsumableManager.
        OnAttackEnded: HeroInventoryUpdated/-Removed); ohne sie behält der
        Client den alten Vorrat und zeigt Tränke, die es nicht mehr gibt."""
        hero = self._hero(hero_id)
        changed = {}  # SlotIndex -> Endstand (0 = entfernt)
        for ability in abilities:
            belt = next((b for b in hero.get("EquippedConsumables") or []
                         if b["StackCount"] > 0
                         and self.data.consumables.get(b["TemplateId"], {})
                         .get("AbilityId") == ability), None)
            stack = belt and next(
                (s for s in self._slots()
                 if s["Item"]["$type"] == T("HeroConsumableItem")
                 and s["Item"].get("TemplateId") == belt["TemplateId"]), None)
            if belt is None or stack is None:
                self.log(f"    [state] Trank (Ability {ability}) benutzt, "
                         "aber keiner im Vorrat")
                continue
            belt["StackCount"] -= 1  # _save gleicht alle Gürtel ab
            stack["StackCount"] -= 1
            changed[stack["SlotIndex"]] = stack["StackCount"]
            if stack["StackCount"] <= 0:
                self._take_slot(stack["SlotIndex"])
            self.log(f"    [state] Trank {belt['TemplateId']} verbraucht, "
                     f"noch {belt['StackCount']}")
        events = [("HeroInventoryRemoved", {"SlotIndex": i}) if n <= 0 else
                  ("HeroInventoryUpdated", {"SlotIndex": i, "StackCount": n})
                  for i, n in changed.items()]
        return events + (self._refill_belt(hero) if changed else [])

    def _refill_belt(self, hero):
        """Gürtel-Slot 0 leer getrunken: nächster Heiltrank mit Vorrat und
        passendem Level (kleinste TemplateId) rückt nach, gemeldet als
        HeroConsumableUnequip + HeroConsumableEquip (wie ConsumableManager.
        OnAttackEnded)."""
        stock = {s["Item"]["TemplateId"] for s in self._slots()
                 if s["Item"]["$type"] == T("HeroConsumableItem")
                 and s["StackCount"] > 0}
        usable = sorted(
            tid for tid in stock
            if self.data.consumables.get(tid, {"$type": ""})["$type"]
            .split(",")[0].endswith("HealthPotionConsumableTemplate")
            and self.data.consumables[tid].get("MinLevelRequirement", 0) <= hero["Level"])
        belt = next((b for b in hero.get("EquippedConsumables") or []
                     if b["SlotIndex"] == 0), None)
        if belt is None or not usable or belt["TemplateId"] in usable:
            return []
        hid = hero["HeroSpecContainerId"]
        self._cmd_HeroEquipConsumableCommand(
            {"HeroId": hid, "TemplateId": usable[0], "EquippedSlotIndex": 0})
        slot = next(b for b in hero["EquippedConsumables"] if b["SlotIndex"] == 0)
        return [("HeroConsumableUnequip", {"HeroSpecContainerId": hid, "SlotIndex": 0}),
                ("HeroConsumableEquip", {"HeroSpecContainerId": hid,
                                         "ConsumableItem": dict(slot)})]

    def _free_slot(self):
        inv = self.account["Inventory"]
        used = {s["SlotIndex"] for s in self._slots()}
        return next((i for i in range(inv["InventoryTabCount"]
                                      * INVENTORY_SLOTS_PER_TAB)
                     if i not in used), None)

    def _add_items(self, items, keep_overflow=True):
        """Legt Items in die niedrigsten freien Inventar-Slots (wie der
        Client); was nicht passt, kommt in den Rückkauf (keep_overflow)
        oder geht verloren."""
        added = []
        for item in items:
            index = self._free_slot()
            if index is None:
                self._overflow(item, 1, keep_overflow)
                continue
            self._put_slot(index, item)
            added.append(copy.deepcopy(self._slots()[-1]))
            self.log(f"    [state] Beute: Template {item['TemplateId']} "
                     f"(Level {item['ItemLevel']}, {len(item['Effects'])} Effekte) "
                     f"-> Slot {index}")
        return added

    def _hero(self, hero_id):
        for h in self.account["Heroes"]:
            if h["HeroSpecContainerId"] == hero_id:
                return h
        raise KeyError(f"Held {hero_id} unbekannt")

    def _slots(self):
        return self.account["Inventory"]["HeroItems"]

    def _take_slot(self, index):
        for i, s in enumerate(self._slots()):
            if s["SlotIndex"] == index:
                return self._slots().pop(i)
        return None

    def _put_slot(self, index, item, count=1):
        if self._take_slot(index) is not None:
            self.log(f"    [state] Inventar-Slot {index} überschrieben")
        self._slots().append({"$type": T("AccountInventorySlot"),
                              "Item": item, "SlotIndex": index,
                              "StackCount": count})

    def _item_from_template(self, template_id, level=None):
        """Neues Equipment-Item in Client-Form aus einem HeroItemTemplate;
        ohne `level` auf LevelMax des Templates, Kosmetik (Pets,
        Kostüme: Spanne 1-30, keine Werte) auf LevelMin, sonst wäre sie für
        Helden unter 30 gesperrt.

        PrimaryStatsModifiers/Effect-Level wie die Vollaccount-Vorlagen
        (0.9 bzw. 4 auf Level 30); die exakte Wurfverteilung des Originals
        ist unbekannt.
        """
        t = self.data.templates[template_id]
        level = level or (t.get("LevelMin", 1) if self.data.is_cosmetic(t)
                          else t.get("LevelMax", t.get("LevelMin", 1)))
        return {
            "$type": T("HeroEquipmentItem"),
            "ArchetypeId": t.get("ArchetypeId", 0),
            "DyeTemplateId": 0,
            "Effects": [{"$type": T("ItemEffect"), "Id": e["Id"],
                         "Level": max(1, level * 4 // 30),
                         "Type": e.get("Type", 0)}
                        for e in t.get("Effects", [])],
            "IsBranded": False,
            "IsSellable": True,
            "ItemLevel": level,
            "PrimaryStatsModifiers": [0.9, 0.9, 0.9],
            "TemplateId": template_id,
        }

    # -- Tutorial (Neu-Account) ---------------------------------------------

    # HeroEquipmentDefinitionSpec.DefaultEquipmentTemplateIds -> Equipment-Slot
    DEFAULT_EQUIPMENT_SLOTS: ClassVar[dict[str, str]] = {
        "WeaponTemplateId": "MainHand",
        "HelmTemplateId": "Head",
        "ShouldersTemplateId": "Shoulders",
        "SuitOfArmorTemplateId": "Body",
        "GlovesTemplateId": "Hands"}

    def _default_equipment(self, hero_spec):
        """{Equipment-Slot: TemplateId} der Standard-Ausrüstung eines Helden."""
        defaults = _equipment_spec(hero_spec).get("DefaultEquipmentTemplateIds") or {}
        return {slot: defaults[key] for key, slot in self.DEFAULT_EQUIPMENT_SLOTS.items()
                if defaults.get(key)}

    def choose_display_name(self, name):
        with self.lock:
            self.account["DisplayName"] = name
            self.account["BuildInfo"]["Draft"]["AccountDisplayName"] = name
            self._save()
        self.log(f"    [state] Name: {name}")

    def _new_hero(self, spec_id):
        """Neuer Held in Client-Form.

        Startwerte aus HeroTemplates/<Default ...> (Level, XP, Tränke),
        Ausrüstung aus Heroes/<Held>/Gameplay.json DefaultEquipmentTemplateIds
        (die "Default ..."-Items; so auch der MQELOffline-Nachbau)."""
        template = _load_json(glob.glob(os.path.join(
            SETTINGS, "HeroTemplates", f"{spec_id:06d} - *.json"))[0])
        equipment: dict[str, Any] = {s: None for s in ("Back", "Body", "Costume", "Finger1",
                                       "Finger2", "Hands", "Head", "MainHand",
                                       "Neck", "OffHand", "Pet", "Shoulders")}
        for slot, tid in self._default_equipment(spec_id).items():
            equipment[slot] = self._item_from_template(tid)
        regions = [dict(r) for r in self.account.get("AttackRegions") or []]
        return {
            "$type": T("Hero"),
            "HeroSpecContainerId": spec_id,
            "XP": template.get("XP", 0),
            "Level": template.get("Level", 1),
            "Attributes": {"$type": T("HeroAttributes"), "Energy": 0,
                           "Magic": 0, "Power": 0, "Vitality": 0},
            "Equipment": equipment,
            "EquippedSpells": [],
            "EquippedConsumables": [
                {"$type": T("HeroConsumableSlot"), "SlotIndex": c.get("SlotIndex", i),
                 "StackCount": c.get("StackCount", 1), "TemplateId": c["TemplateId"]}
                for i, c in enumerate(template.get("EquippedConsumables") or [])],
            "Stats": {"$type": T("AccountHeroStats"), "TimePlayed": 0,
                      "TotalCastlesLooted": 0, "TotalCreaturesKilled": 0},
            "HeroStatModifier": {},
            "OwnerSpecialPacks": [],
            "AttackRegions": regions,
        }

    def choose_first_hero(self, spec_id):
        """Legt den ersten Helden an (HeroService.ChooseFirstHero) und
        liefert ihn in Client-Form. Privileges danach 401 wie bei
        MQELOffline (vorher 9)."""
        with self.lock:
            hero = self._new_hero(spec_id)
            self.account["Heroes"] = [h for h in self.account["Heroes"]
                                      if h["HeroSpecContainerId"] != spec_id] + [hero]
            self.account["SelectedHeroId"] = spec_id
            self.account["Privileges"] = 401
            self._unlock_spells()
            self._save()
        self.log(f"    [state] erster Held: {spec_id}")
        return copy.deepcopy(hero)

    # -- Assignments (Tutorial-/Aufgabenschritte) ------------------------------
    # Wie MQELOffline (Backend/Questing.cpp): abgeschlossene Assignments
    # kommen per CompletedAssignments in GetAccountInformation zurück; ein
    # gestartetes, nicht abgeschlossenes beginnt nach Neustart von vorn.

    def _cmd_StartAssignmentCommand(self, cmd):
        self.log(f"    [state] Assignment {cmd['AssignmentId']} gestartet")

    def _cmd_CompleteAssignmentCommand(self, cmd):
        done = self.account.setdefault("CompletedAssignments", [])
        if cmd["AssignmentId"] not in done:
            done.append(cmd["AssignmentId"])
        self.log(f"    [state] Assignment {cmd['AssignmentId']} abgeschlossen")

    def _consume_renovation_cost(self, level):
        """Kosten der Renovierungsstufe (CastleRenovationSettings, Cost
        [{TemplateId, StackCount}]) aus der Materialsammlung
        (Inventory.CastleRenovationItems) abziehen: die Stufe wird
        automatisch ausgeführt, sobald die Sammlung vollständig ist."""
        info = _load_json(os.path.join(SETTINGS, "GeneralSettings",
                                       "CastleRenovationSettings.json"))
        cost = (info["PerLevelRenovationInformation"].get(level) or {}).get("Cost") or []
        items = self.account["Inventory"].setdefault("CastleRenovationItems", [])
        for c in cost:
            need = c["StackCount"]
            for slot in [x for x in items
                         if x["Item"].get("TemplateId") == c["TemplateId"]]:
                take = min(need, slot.get("StackCount", 1))
                slot["StackCount"] = slot.get("StackCount", 1) - take
                need -= take
                if slot["StackCount"] <= 0:
                    items.remove(slot)
                if need == 0:
                    break
            if need:
                self.log(f"    [state] Renovierung: {need}x Material "
                         f"{c['TemplateId']} fehlte in der Sammlung")
        self.log(f"    [state] Renovierungs-Material verbraucht: {cost}")

    def _cmd_ExecuteAssignmentActionCommand(self, cmd):
        """Serverseitige Aktion eines Assignments: ausführen und bestätigen
        (ServerAssignmentActionCompleted, NotificationType 74; darauf wartet
        der Client)."""
        action = _assignment_action(cmd["AssignmentId"], cmd["ActionIndex"]) or {}
        kind = action["$type"].split(",")[0].rsplit(".", 1)[-1] if action else None
        if kind == "SetCastleRenovationLevelAssignmentActionSpec":
            # Account.CastleRenovationLevel (DefaultAccount: "RenovationLevel0");
            # ohne Speichern startet der Client nach Neustart wieder bei 0 und
            # wartet im Tutorial (5007) ewig auf Stufe 2
            self.account["CastleRenovationLevel"] = action["CastleRenovationLevel"]
            self.log(f"    [state] Burg-Renovierung: {action['CastleRenovationLevel']}")
            self._consume_renovation_cost(action["CastleRenovationLevel"])
        elif kind == "GrantPrivilegesAssignmentActionSpec":
            self.account["Privileges"] = (self.account.get("Privileges", 0)
                                          | action["AccountPrivileges"])
            self._grant_castle_shareable()
            self.log(f"    [state] Privilegien: {self.account['Privileges']}")
        elif kind == "AddItemsToInventoryAssignmentActionSpec":
            # z. B. 4000 "Creature crafting tutorial": Material 1000-1002
            items = [("consumable", i["TemplateId"], i.get("StackCount", 1))
                     if i["$type"] == T("HeroConsumableItem")
                     else ("equipment", i["TemplateId"], i.get("ItemLevel", 1))
                     for i in action.get("InventoryItems", [])]
            occupied = self._occupied()
            self.events += self._inventory_events(self._add_reward_items(items),
                                                  occupied)
        else:
            self.log(f"    [state] Assignment-Aktion {cmd['AssignmentId']}/"
                     f"{cmd['ActionIndex']} ({kind}) nicht umgesetzt")
        self.events.append(("ServerAssignmentActionCompleted", {
            "AssignmentId": cmd["AssignmentId"],
            "AssignmentActionIndex": cmd["ActionIndex"]}))

    # -- Objectives (Quests) ---------------------------------------------------
    # Assignments schalten Quests clientseitig frei (UnlockObjective-Aktion)
    # und melden das per ObjectiveUnlockCommand; ohne Eintrag kennt der
    # Server die Quest nicht, und der Client sperrt nach Neustart die Burg.

    def _objective(self, objective_id):
        return next((o for o in self.account.setdefault("Objectives", [])
                     if o["ObjectiveId"] == objective_id), None)

    def _cmd_ObjectiveUnlockCommand(self, cmd):
        if self._objective(cmd["ObjectiveId"]) is not None:
            return
        now = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.account["Objectives"].append({
            "$type": T("AccountObjective"), "HasViewed": False,
            "LastStatusDate": now, "ObjectiveId": cmd["ObjectiveId"],
            "ProgressionCount": 0, "Status": objectives.UNLOCKED})
        self.log(f"    [state] Objective {cmd['ObjectiveId']} freigeschaltet")

    def _cmd_ObjectiveViewedCommand(self, cmd):
        o = self._objective(cmd["ObjectiveId"])
        if o is not None:
            o["HasViewed"] = True

    def _cmd_SpellViewedCommand(self, cmd):
        sid = cmd["Spell"]["SpellSpecContainerId"]
        for s in self.account.get("UnlockedSpells", []):
            if s["SpellSpecContainerId"] == sid:
                s["NewlyAdded"] = False

    def _cmd_HeroEquipSpellCommand(self, cmd):
        # SlotIndex 0 (erster Slot) lässt der Client weg, wie alle Nullwerte
        index = cmd.get("SlotIndex", 0)
        hero = self._hero(cmd["HeroId"])
        slots = [x for x in hero.get("EquippedSpells") or []
                 if x.get("SlotIndex", 0) != index
                 and x["SpellSpecContainerId"] != cmd["SpellId"]]
        slots.append({"$type": T("HeroSpellSlot"), "SlotIndex": index,
                      "SpellSpecContainerId": cmd["SpellId"]})
        hero["EquippedSpells"] = sorted(slots, key=lambda x: x.get("SlotIndex", 0))
        self._unlock_equipped_spells()
        self.log(f"    [state] Held {cmd['HeroId']}: Zauber {cmd['SpellId']} "
                 f"in Slot {index}")

    def _cmd_SetLastViewedDateCommand(self, cmd):
        """Quittung des Clients (Type 1 = Clan-Begrüßung gezeigt) ->
        Account.LastViewedDates [ViewableItem {Type, LastViewedDate}]
        (Leser 0x8cc2a0; ein unbekannter $type -> 0x80030003). Ohne Eintrag zeigt er "Welcome to your new clan!"
        bei jedem Account-Laden."""
        dates = [x for x in self.account.get("LastViewedDates") or []
                 if x["Type"] != cmd["Type"]]
        dates.append({"$type": T("ViewableItem"), "Type": cmd["Type"],
                      "LastViewedDate": cmd["ViewedDate"]})
        self.account["LastViewedDates"] = dates
        self.log(f"    [state] LastViewedDates[{cmd['Type']}] = {cmd['ViewedDate']}")

    def _cmd_HeroUnequipSpellCommand(self, cmd):
        hero = self._hero(cmd["HeroId"])
        hero["EquippedSpells"] = [x for x in hero.get("EquippedSpells") or []
                                  if x.get("SlotIndex", 0) != cmd.get("SlotIndex", 0)]
        self.log(f"    [state] Held {cmd['HeroId']}: Zauber-Slot {cmd.get('SlotIndex', 0)} geleert")

    def _cmd_SetProfanityFilteringCommand(self, cmd):
        self.account["ProfanityFiltering"] = bool(cmd.get("Enabled"))

    def take_unlocked_spells(self):
        """Seit dem letzten Aufruf freigeschaltete Zauber (für Notifications)."""
        with self.lock:
            out, self.spells_unlocked = self.spells_unlocked, []
            return out

    def _cmd_BuyCommand(self, cmd):
        """Kauf per SKU; bisher nur ItemType 21 = CastleForSale (Startburg im
        Tutorial, Assignment 20 "BuyRandomCastle" wartet auf CastleBought)."""
        sku = self.data.skus[cmd["SkuCode"]]
        if sku["ItemType"] not in (1, 5, 6, 9, 14, 15, 18, 21, 22):
            raise ValueError(f"BuyCommand: SKU-ItemType {sku['ItemType']} "
                             "nicht unterstützt")
        if sku["ItemType"] in (5, 6):  # ItemType.Creature / Trap
            self._buy_defense_ingredient(sku, cmd.get("ConsumedHeroInventory"))
            return
        self._consume_limited(sku)
        if sku["ItemType"] == 1:  # ItemType.Hero
            self._buy_hero(sku)
            return
        if sku["ItemType"] == 9:  # ItemType.InventoryTabs
            self._buy_inventory_tab(sku)
            return
        if sku["ItemType"] == 14:  # ItemType.ShowMoreCastle
            self._buy_show_more_castle(sku)
            return
        if sku["ItemType"] == 15:  # ItemType.CastleTheme
            self._buy_theme(sku)
            return
        if sku["ItemType"] == 18:  # Zutatenpaket (ItemId = Paket-Id)
            self._buy_material_pack(sku)
            return
        if sku["ItemType"] == 22:  # CastleValidationDelegation
            self._buy_validation_delegation(sku)
            return
        if sku["ItemType"] != 21:
            raise ValueError(f"BuyCommand: SKU-ItemType {sku['ItemType']} "
                             "nicht unterstützt")
        now = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        fields = castle_sale.bought_castle(sku["ItemId"], self.account["AccountId"],
                                           self.account.get("DisplayName", ""), now)
        if fields is None:
            raise KeyError(f"Kaufburg {sku['ItemId']} unbekannt")
        build = self.account["BuildInfo"]
        build.update(copy.deepcopy(fields))
        self.account[self.SALE] = sku["ItemId"]
        self.events.append(("CastleBought", {
            "IsStartupCastle": True,
            "BuildInfo": copy.deepcopy(build)}))
        self.log(f"    [state] Burg gekauft: Kaufburg {sku['ItemId']} "
                 f"(Thema {fields['Draft']['ThemeId']})")

    def _buy_inventory_tab(self, sku):
        """Inventar-Reiter kaufen (InventoryManager.PurchaseInventoryTab):
        ItemCount ist die neue Reiterzahl, also genau der nächste Reiter."""
        inv = self.account["Inventory"]
        if sku["ItemCount"] != inv["InventoryTabCount"] + 1 \
                or sku["ItemCount"] > MAX_INVENTORY_TABS:
            raise ValueError(f"Reiter {sku['ItemCount']} passt nicht zu "
                             f"{inv['InventoryTabCount']} vorhandenen")
        price = sku.get("Price") or {}
        if price:
            field = CURRENCY[price["CurrencyType"]]
            if self.account["Wallet"][field] < price["Amount"]:
                raise ValueError(f"zu wenig {field}")
            self.account["Wallet"][field] -= price["Amount"]
        inv["InventoryTabCount"] = sku["ItemCount"]
        self.log(f"    [state] Inventar-Reiter {sku['ItemCount']} gekauft, "
                 f"-{price.get('Amount', 0)}")

    def _buy_theme(self, sku):
        """Burg-Theme freischalten (BuildInfo.InventoryThemes)."""
        theme = sku["ItemId"]
        themes = self.account["BuildInfo"].setdefault("InventoryThemes", [])
        if theme in themes:
            raise ValueError(f"Theme {theme} schon freigeschaltet")
        price = sku.get("Price") or {}
        if price:
            field = CURRENCY[price["CurrencyType"]]
            if self.account["Wallet"][field] < price["Amount"]:
                raise ValueError(f"zu wenig {field}")
            self.account["Wallet"][field] -= price["Amount"]
        themes.append(theme)
        self.events.append(("ThemeUnlocked", {"ThemeId": theme}))
        self.log(f"    [state] Theme {theme} freigeschaltet, -{price.get('Amount', 0)}")

    def _buy_show_more_castle(self, sku):
        """"Reroll castles in the region" (ItemId = Angriffsregion): neue
        Burgauswahl würfeln. Der Zähler je Region bleibt im Account, damit
        GetAttackSelectionList danach dieselbe Auswahl liefert."""
        price = sku.get("Price") or {}
        if price:
            field = CURRENCY[price["CurrencyType"]]
            if self.account["Wallet"][field] < price["Amount"]:
                raise ValueError(f"zu wenig {field}")
            self.account["Wallet"][field] -= price["Amount"]
        region = sku["ItemId"]
        rerolls = self.account.setdefault("CastleRerolls", {})
        index = rerolls.get(str(region), 0) + 1
        rerolls[str(region)] = index
        # AttackSelectionResult setzt der Aufrufer außerhalb der Sperre ein
        # (game_server: die Auswahl liest die Spielstände aller Spieler,
        # auch diesen -- in der Sperre hängt der Server)
        self.events.append(("ShowMoreCastle", {"RegionId": region, "Index": index}))
        self.log(f"    [state] Region {region} neu gewürfelt (#{index}), "
                 f"-{price.get('Amount', 0)}")

    def _buy_material_pack(self, sku):
        """Zutatenpaket: Preis abziehen, Inhalt würfeln, ins Helden-Inventar.
        Der Client wartet auf CraftingMaterialsPackPurchased (Liste
        CraftingMaterial {Id, Quantity}) und zeigt den Inhalt an."""
        price = sku.get("Price") or {}
        if price:
            field = CURRENCY[price["CurrencyType"]]
            if self.account["Wallet"][field] < price["Amount"]:
                raise ValueError(f"zu wenig {field}")
            self.account["Wallet"][field] -= price["Amount"]
        gained = item_drops.material_pack(sku["ItemId"])
        # Die Paket-Meldung zeigt nur den Inhalt; ins Inventar bucht der
        # Client erst per HeroInventoryAdded/-Updated
        occupied = self._occupied()
        self.events += self._inventory_events(
            self._add_reward_items([("material", tid, n) for tid, n in gained]),
            occupied)
        self.events.append(("CraftingMaterialsPackPurchased", {
            "CraftingMaterialsGained": [
                {"$type": T("CraftingMaterial"), "Id": tid, "Quantity": n}
                for tid, n in gained]}))
        self.log(f"    [state] Zutatenpaket {sku['ItemId']}: {gained}, "
                 f"-{price.get('Amount', 0)}")

    def _buy_defense_ingredient(self, sku, consumed):
        """Kreatur (ItemType 5) bzw. Falle (6) für die eigene Burg: erhöht
        AvailableCount des Archetyps in BuildInfo und legt die Stufe im
        Draft an (Client liest die Stufe aus Draft.*Tiers). Das Material der
        SKU (CraftingMaterials) bucht der Client als ConsumedHeroInventory
        mit; es muss genau den Kosten entsprechen."""
        price = sku.get("Price") or {}
        field = CURRENCY[price["CurrencyType"]] if price else None
        wallet = self.account["Wallet"]
        if field and wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        need = collections.Counter()
        for m in sku.get("CraftingMaterials") or []:
            need[m["Id"]] += m["Quantity"]
        paid = collections.Counter()
        for u in consumed or []:
            paid[u["TemplateId"]] -= u["Count"]
        if paid != need:
            raise ValueError(f"SKU {sku['Code']}: Material {dict(paid)} "
                             f"statt {dict(need)}")
        slots = {x["SlotIndex"]: x for x in self._slots()}
        for u in consumed or []:
            slot = slots.get(u["SlotIndex"])
            if (slot is None or slot["Item"].get("TemplateId") != u["TemplateId"]
                    or slot.get("StackCount", 1) < -u["Count"]):
                raise ValueError(f"Material {u['TemplateId']} fehlt in Platz "
                                 f"{u['SlotIndex']}")
        self._consume_limited(sku)
        self._consume_inventory(consumed)
        if field:
            wallet[field] -= price["Amount"]
        kind = "Creature" if sku["ItemType"] == 5 else "Trap"
        spec = sku["ItemId"]
        n = self._add_archetype(kind, spec, 1)  # ItemCount = Stufe, nicht Anzahl
        self.log(f"    [state] gekauft: {kind} {spec} (jetzt {n}), "
                 f"-{price.get('Amount', 0)} {field}")

    def _add_archetype(self, kind, spec, count):
        """Kreaturen ("Creature") bzw. Fallen ("Trap") in den Vorrat
        (BuildInfo.<kind>Archetypes, Stufe im Draft); neuer Bestand."""
        build = self.account["BuildInfo"]
        archetypes = build.setdefault(kind + "Archetypes", [])
        a = next((x for x in archetypes if x["SpecContainerId"] == spec), None)
        if a is None:
            a = {"$type": T("DefenseIngredientArchetype"), "AvailableCount": 0,
                 "SpecContainerId": spec, "Specialization": 0, "Tier": 1}
            archetypes.append(a)
        a["AvailableCount"] += count
        tiers = build["Draft"].setdefault(kind + "Tiers", [])
        if not any(t["SpecContainerId"] == spec for t in tiers):
            tiers.append({"$type": T("DefenseIngredientTier"), "SpecContainerId": spec,
                          "Specialization": 0, "Tier": 1})
        return a["AvailableCount"]

    def _cmd_AddCastleCreatureCommand(self, cmd):
        """Kreatur in der eigenen Burg platzieren. Mit SkuCode kauft der
        Client sie dabei (Kontingent + Preis, z. B. "[CE] Chicken_Grunt"),
        sonst kommt sie aus dem Vorrat (AvailableCount)."""
        build = self.account["BuildInfo"]
        room = next((r for r in build["Draft"]["Rooms"] if r["Id"] == cmd["RoomId"]), None)
        if room is None:
            raise KeyError(f"Raum {cmd['RoomId']} unbekannt")
        spec = cmd["SpecContainerId"]
        if cmd.get("SkuCode"):
            sku = self.data.skus[cmd["SkuCode"]]
            if sku["ItemType"] != 5 or sku["ItemId"] != spec:
                raise ValueError(f"SKU {cmd['SkuCode']} passt nicht zu Kreatur {spec}")
            self._buy_defense_ingredient(sku, cmd.get("ConsumedHeroInventory"))
        a = next((x for x in build.get("CreatureArchetypes", [])
                  if x["SpecContainerId"] == spec), None)
        if a is None or a["AvailableCount"] < 1:
            raise ValueError(f"Kreatur {spec} nicht vorrätig")
        a["AvailableCount"] -= 1
        room.setdefault("Creatures", []).append({
            "$type": T("CastleCreature"),
            "AggroPropagationOffsetX": cmd.get("AggroPropagationOffsetX", 0.0),
            "AggroPropagationOffsetZ": cmd.get("AggroPropagationOffsetZ", 0.0),
            "IsSleeping": False, "TotemCastleBuildableId": cmd.get("TotemCastleBuildableId", 0),
            "BoostId": 0, "RoomZoneId": cmd.get("RoomZoneId", 0), "Id": cmd["Id"],
            "Orientation": cmd.get("Orientation", 0), "SkuCode": "",
            "SpecContainerId": spec, "X": cmd.get("X", 0), "Y": cmd.get("Y", 0)})
        build["CreatureNextIndex"] = max(build.get("CreatureNextIndex", 0), cmd["Id"] + 1)
        self.log(f"    [state] Kreatur {spec} (Id {cmd['Id']}) in Raum {cmd['RoomId']} gesetzt")

    def _room(self, room_id):
        room = next((r for r in self.account["BuildInfo"]["Draft"]["Rooms"]
                     if r["Id"] == room_id), None)
        if room is None:
            raise KeyError(f"Raum {room_id} unbekannt")
        return room

    def _cmd_AddCastleTrapCommand(self, cmd):
        """Falle platzieren, wie Kreaturen: mit SkuCode gekauft (ItemType 6,
        z. B. "[CE] Rotating Cannon 1 Flame"), sonst aus dem Vorrat."""
        build = self.account["BuildInfo"]
        room = self._room(cmd["RoomId"])
        spec = cmd["SpecContainerId"]
        if cmd.get("SkuCode"):
            sku = self.data.skus[cmd["SkuCode"]]
            if sku["ItemType"] != 6 or sku["ItemId"] != spec:
                raise ValueError(f"SKU {cmd['SkuCode']} passt nicht zu Falle {spec}")
            self._buy_defense_ingredient(sku, cmd.get("ConsumedHeroInventory"))
        a = next((x for x in build.get("TrapArchetypes", [])
                  if x["SpecContainerId"] == spec), None)
        if a is None or a["AvailableCount"] < 1:
            raise ValueError(f"Falle {spec} nicht vorrätig")
        a["AvailableCount"] -= 1
        room.setdefault("Traps", []).append({
            "$type": T("CastleTrap"), "BeatIndex": cmd.get("BeatIndex", 0),
            "Id": cmd["Id"], "Orientation": cmd.get("Orientation", 0),
            "PowerSupplyCastleBuildableId": cmd.get("PowerSupplyCastleBuildableId", 0),
            "RoomZoneId": cmd.get("RoomZoneId", 0), "SkuCode": "",
            "SpecContainerId": spec, "X": cmd.get("X", 0), "Y": cmd.get("Y", 0)})
        build["TrapNextIndex"] = max(build.get("TrapNextIndex", 0), cmd["Id"] + 1)
        self.log(f"    [state] Falle {spec} (Id {cmd['Id']}) in Raum {cmd['RoomId']} gesetzt")

    def _return_to_inventory(self, key, archetypes, cmd):
        """Kreatur/Falle ins Inventar zurück: aus dem Raum nehmen, Vorrat
        (<archetypes>.AvailableCount) +1. Ein Boost darauf verfällt."""
        build = self.account["BuildInfo"]
        room = next((r for r in build["Draft"]["Rooms"]
                     for x in r.get(key) or [] if x["Id"] == cmd["Id"]), None)
        if room is None:
            raise KeyError(f"{key} {cmd['Id']} unbekannt")
        item = next(x for x in room[key] if x["Id"] == cmd["Id"])
        room[key].remove(item)
        spec = item["SpecContainerId"]
        a = next((x for x in build.setdefault(archetypes, [])
                  if x["SpecContainerId"] == spec), None)
        if a is None:
            a = {"$type": T("DefenseIngredientArchetype"), "AvailableCount": 0,
                 "SpecContainerId": spec, "Specialization": 0, "Tier": 1}
            build[archetypes].append(a)
        a["AvailableCount"] += 1
        item_type = {"Creatures": 5, "Traps": 6}[key]
        self.account["Expirables"] = [
            e for e in self.account.get("Expirables") or []
            if not (e.get("ExpirableType") == "DefenseIngredientBoost"
                    and e.get("DefenseIngredientId") == cmd["Id"]
                    and e.get("DefenseItemType", 5) == item_type)]
        self.log(f"    [state] {key} {spec} (Id {cmd['Id']}) ins Inventar")

    def _cmd_RemoveCastleCreatureCommand(self, cmd):
        self._return_to_inventory("Creatures", "CreatureArchetypes", cmd)

    def _cmd_RemoveCastleTrapCommand(self, cmd):
        self._return_to_inventory("Traps", "TrapArchetypes", cmd)
        # Fallen an einem entfernten Generator haben keinen Strom mehr
        for r in self.account["BuildInfo"]["Draft"]["Rooms"]:
            for t in r.get("Traps") or []:
                if t.get("PowerSupplyCastleBuildableId") == cmd["Id"]:
                    t["PowerSupplyCastleBuildableId"] = 0

    def _cmd_AddCastleDecorationCommand(self, cmd):
        """Deko setzen: mit SkuCode gekauft (ItemType 13, z. B. 15151
        Barricade, 50 Blings), sonst aus InventoryDecorations. Kontingent-SKUs
        ("[CE] ..." aus den Burg-Level-Paketen, Infinite Pack) zählen in
        ShopSkuConsumed, sonst bot der Shop sie nach dem Login wieder an."""
        build = self.account["BuildInfo"]
        spec = cmd["SpecContainerId"]
        if cmd.get("SkuCode"):
            sku = self.data.skus[cmd["SkuCode"]]
            if sku["ItemType"] != 13 or sku["ItemId"] != spec:
                raise ValueError(f"SKU {cmd['SkuCode']} passt nicht zu Deko {spec}")
            price = sku.get("Price") or {}
            field = CURRENCY[price["CurrencyType"]] if price else None
            if field and self.account["Wallet"][field] < price["Amount"]:
                raise ValueError(f"zu wenig {field}")
            self._consume_limited(sku)
            if field:
                self.account["Wallet"][field] -= price["Amount"]
        else:
            inv = next((x for x in build.get("InventoryDecorations") or []
                        if x["SpecContainerId"] == spec and x["AvailableCount"] > 0), None)
            if inv is None:
                raise ValueError(f"Deko {spec} nicht vorrätig")
            inv["AvailableCount"] -= 1
        self._consume_inventory(cmd.get("ConsumedHeroInventory"))
        self._room(cmd["RoomId"]).setdefault("Decorations", []).append({
            "$type": T("CastleDecoration"), "BoostId": 0,
            "RoomZoneId": cmd.get("RoomZoneId", 0), "Id": cmd["Id"],
            "Orientation": cmd.get("Orientation", 0), "SkuCode": "",
            "SpecContainerId": spec, "X": cmd.get("X", 0), "Y": cmd.get("Y", 0)})
        build["DecorationNextIndex"] = max(build.get("DecorationNextIndex", 0), cmd["Id"] + 1)
        self.log(f"    [state] Deko {spec} (Id {cmd['Id']}) in Raum {cmd['RoomId']} gesetzt")

    def _cmd_RemoveCastleDecorationCommand(self, cmd):
        """Deko ins Inventar (InventoryDecorations.AvailableCount +1)."""
        build = self.account["BuildInfo"]
        room = next((r for r in build["Draft"]["Rooms"]
                     for x in r.get("Decorations") or [] if x["Id"] == cmd["Id"]), None)
        if room is None:
            raise KeyError(f"Deko {cmd['Id']} unbekannt")
        deco = next(x for x in room["Decorations"] if x["Id"] == cmd["Id"])
        room["Decorations"].remove(deco)
        spec = deco["SpecContainerId"]
        self._add_inventory_decoration(spec)
        self.log(f"    [state] Deko {spec} (Id {cmd['Id']}) ins Inventar")

    def _add_inventory_decoration(self, spec):
        """Eine Deko ins Burg-Inventar (InventoryDecorations)."""
        build = self.account["BuildInfo"]
        inv = next((x for x in build.setdefault("InventoryDecorations", [])
                    if x["SpecContainerId"] == spec), None)
        if inv is None:
            inv = {"$type": T("InventoryDecoration"), "AvailableCount": 0,
                   "Level": 1, "SpecContainerId": spec}
            build["InventoryDecorations"].append(inv)
        inv["AvailableCount"] += 1

    def _cmd_UpdateCastleDecorationCommand(self, cmd):
        """Deko verschieben/drehen (auch in einen anderen Raum)."""
        rooms = self.account["BuildInfo"]["Draft"]["Rooms"]
        old = next((r for r in rooms for x in r.get("Decorations") or []
                    if x["Id"] == cmd["Id"]), None)
        if old is None:
            raise KeyError(f"Deko {cmd['Id']} unbekannt")
        deco = next(x for x in old["Decorations"] if x["Id"] == cmd["Id"])
        for k in ("Orientation", "RoomZoneId", "X", "Y"):
            if k in cmd:
                deco[k] = cmd[k]
        if cmd.get("RoomId", old["Id"]) != old["Id"]:
            old["Decorations"].remove(deco)
            self._room(cmd["RoomId"]).setdefault("Decorations", []).append(deco)
        self.log(f"    [state] Deko {cmd['Id']} verschoben")

    def _cmd_SetAvatarCommand(self, cmd):
        """Avatar wählen ({AvatarId})."""
        avatar = cmd.get("AvatarId", cmd.get("Id"))
        if avatar is None:
            raise KeyError(f"SetAvatarCommand ohne AvatarId: {cmd}")
        self.account["AvatarId"] = avatar
        self.log(f"    [state] Avatar {avatar}")

    def _cmd_UpdateCastleTrapCommand(self, cmd):
        """Falle ändern: verschieben (auch in einen anderen Raum), drehen,
        mit einem Generator verbinden (PowerSupplyCastleBuildableId)."""
        rooms = self.account["BuildInfo"]["Draft"]["Rooms"]
        old = next((r for r in rooms for t in r.get("Traps") or []
                    if t["Id"] == cmd["Id"]), None)
        if old is None:
            raise KeyError(f"Falle {cmd['Id']} unbekannt")
        trap = next(t for t in old["Traps"] if t["Id"] == cmd["Id"])
        for k in ("BeatIndex", "Orientation", "PowerSupplyCastleBuildableId",
                  "RoomZoneId", "X", "Y"):
            if k in cmd:
                trap[k] = cmd[k]
        if cmd.get("RoomId", old["Id"]) != old["Id"]:
            old["Traps"].remove(trap)
            self._room(cmd["RoomId"]).setdefault("Traps", []).append(trap)
        self.log(f"    [state] Falle {cmd['Id']} geändert (Raum {cmd.get('RoomId')}, "
                 f"Generator {trap['PowerSupplyCastleBuildableId']})")

    def _find_buildable(self, key, bid):
        """(Raum, Objekt) mit Id <bid> in Draft.Rooms[*].<key>."""
        for r in self.account["BuildInfo"]["Draft"]["Rooms"]:
            for x in r.get(key) or []:
                if x["Id"] == bid:
                    return r, x
        raise KeyError(f"{key} {bid} unbekannt")

    def _move_buildable(self, key, cmd, fields):
        """Felder <fields> aus cmd übernehmen, ggf. in Raum cmd.RoomId umziehen."""
        old, x = self._find_buildable(key, cmd["Id"])
        for k in fields:
            if k in cmd:
                x[k] = cmd[k]
        if cmd.get("RoomId", old["Id"]) != old["Id"]:
            old[key].remove(x)
            self._room(cmd["RoomId"]).setdefault(key, []).append(x)
        return x

    def _cmd_UpdateCastleCreatureCommand(self, cmd):
        """Kreatur verschieben/drehen, Schlafen, Aggro-Versatz."""
        self._move_buildable("Creatures", cmd, (
            "AggroPropagationOffsetX", "AggroPropagationOffsetZ", "IsSleeping",
            "Orientation", "RoomZoneId", "X", "Y"))
        self.log(f"    [state] Kreatur {cmd['Id']} geändert")

    def _cmd_AddCastleTriggerCommand(self, cmd):
        """Auslöser setzen. Es gibt keinen ItemType dafür, also kostenlos."""
        build = self.account["BuildInfo"]
        self._room(cmd["RoomId"]).setdefault("Triggers", []).append({
            "$type": T("CastleTrigger"), "Id": cmd["Id"],
            "Orientation": cmd.get("Orientation", 0), "RoomZoneId": cmd.get("RoomZoneId", 0),
            "SizeX": cmd.get("SizeX", 1), "SizeY": cmd.get("SizeY", 1), "SkuCode": "",
            "SpecContainerId": cmd["SpecContainerId"], "X": cmd.get("X", 0),
            "Y": cmd.get("Y", 0)})
        build["TriggerNextIndex"] = max(build.get("TriggerNextIndex", 0), cmd["Id"] + 1)
        self.log(f"    [state] Auslöser {cmd['SpecContainerId']} (Id {cmd['Id']}) gesetzt")

    def _cmd_UpdateCastleTriggerCommand(self, cmd):
        self._move_buildable("Triggers", cmd,
                             ("Orientation", "RoomZoneId", "SizeX", "SizeY", "X", "Y"))
        self.log(f"    [state] Auslöser {cmd['Id']} geändert")

    def _cmd_RemoveCastleTriggerCommand(self, cmd):
        room, x = self._find_buildable("Triggers", cmd["Id"])
        room["Triggers"].remove(x)
        self.log(f"    [state] Auslöser {cmd['Id']} entfernt")

    def _cmd_RemoveBuildingCommand(self, cmd):
        """Gebäude abreißen. Es gibt kein Gebäude-Inventar und keine
        Erstattung; der Minenstatus fällt mit weg."""
        room, b = self._find_buildable("Buildings", cmd["Id"])
        room["Buildings"].remove(b)
        self.account["BuildInfo"].get("MineStatuses", {}).pop(str(cmd["Id"]), None)
        self.log(f"    [state] Gebäude {b['SpecContainerId']} (Id {cmd['Id']}) abgerissen")

    def _cmd_SellDefenseIngredientCommand(self, cmd):
        """Kreaturen (ItemType 5)/Fallen (6) bzw. Inventar-Deko (13)
        verkaufen. Erlös wie im finalen Client (und BuildManager.SellItem):
        Kreatur/Falle = LifeForce-SKU-Preis der aktuellen Stufe (ItemCount =
        Tier) × SellDefenseIngredientPriceModifier in LifeForce, Deko =
        Blings-Preis × SellPremiumCashIGCRatio × …PremiumCashModifier in
        Gold; je Stück floor(x + 0,5)."""
        item_type, spec = cmd["ItemType"], cmd["ItemId"]
        count = cmd.get("Count", 1)
        if count <= 0:
            raise ValueError(f"ungültige Anzahl {count}")
        build = self.account["BuildInfo"]
        kind = {5: "CreatureArchetypes", 6: "TrapArchetypes",
                13: "InventoryDecorations"}[item_type]
        a = next((x for x in build.get(kind, []) if x["SpecContainerId"] == spec), None)
        if a is None or a["AvailableCount"] < count:
            raise ValueError(f"{kind} {spec}: nicht {count}x vorrätig")
        shop = self.data.shop
        if item_type == 13:
            currency, field = 1, "InGameCoin"
            factor = shop["SellPremiumCashIGCRatio"] * shop["SellDefenseIngredientPricePremiumCashModifier"]
        else:
            currency, field = 4, "LifeForce"
            factor = shop["SellDefenseIngredientPriceModifier"]
        skus = [x for x in self.data.skus.values()
                if x.get("ItemType") == item_type and x.get("ItemId") == spec
                and (x.get("Price") or {}).get("CurrencyType") == currency
                and (item_type == 13 or x.get("ItemCount", 1) == a.get("Tier", 1))]
        skus.sort(key=lambda x: not x.get("IsActive"))
        each = math.floor(skus[0]["Price"]["Amount"] * factor + 0.5) if skus else 0
        a["AvailableCount"] -= count
        self._invalidate_rollback()
        if each:
            self._credit(field, each * count)
        self.log(f"    [state] {count}x {spec} verkauft, +{each * count} {field}")

    def _cmd_UpdateBuildingCommand(self, cmd):
        """Gebäude verschieben/drehen (auch in einen anderen Raum)."""
        rooms = self.account["BuildInfo"]["Draft"]["Rooms"]
        old = next((r for r in rooms for b in r.get("Buildings") or []
                    if b["Id"] == cmd["Id"]), None)
        if old is None:
            raise KeyError(f"Gebäude {cmd['Id']} unbekannt")
        b = next(x for x in old["Buildings"] if x["Id"] == cmd["Id"])
        for k in ("Orientation", "RoomZoneId", "X", "Y"):
            if k in cmd:
                b[k] = cmd[k]
        if cmd.get("RoomId", old["Id"]) != old["Id"]:
            old["Buildings"].remove(b)
            self._room(cmd["RoomId"]).setdefault("Buildings", []).append(b)
        self.log(f"    [state] Gebäude {cmd['Id']} nach Raum {cmd.get('RoomId')} "
                 f"({b['X']}/{b['Y']})")

    def _consume_inventory(self, updates):
        """ConsumedHeroInventory eines Befehls abbuchen: je Eintrag
        {SlotIndex, TemplateId, Count (negativ)}; leere Plätze fallen weg."""
        for u in updates or []:
            slot = next((x for x in self._slots() if x["SlotIndex"] == u["SlotIndex"]
                         and x["Item"].get("TemplateId") == u["TemplateId"]), None)
            if slot is None:
                raise ValueError(f"Material {u['TemplateId']} nicht in Platz {u['SlotIndex']}")
            slot["StackCount"] = slot.get("StackCount", 1) + u["Count"]
            if slot["StackCount"] <= 0:
                self._take_slot(u["SlotIndex"])
            self.log(f"    [state] Platz {u['SlotIndex']}: {u['Count']:+d}x {u['TemplateId']}")

    def _cmd_BoostCastleInventoryItemCommand(self, cmd):
        """Kreatur boosten (SKU ItemType 25, ItemId = Boost-Spec, z. B. 10286
        "Creature Boost - Derp Grunt" für 1x Material 1000): Material
        abbuchen, BoostId an der Kreatur setzen. Die Laufzeit (Boost-Spec
        Duration, 24h) steht im Expirable; abgelaufene räumt expire_boosts()
        weg."""
        sku = self.data.skus[cmd["SkuCode"]]
        if sku["ItemType"] != 25:
            raise ValueError(f"SKU {cmd['SkuCode']} ist kein Boost")
        key = {5: "Creatures", 6: "Traps"}.get(cmd.get("ItemType"), "Creatures")
        target = next((x for r in self.account["BuildInfo"]["Draft"]["Rooms"]
                       for x in r.get(key) or [] if x["Id"] == cmd["Id"]), None)
        if target is None:
            raise KeyError(f"{key} {cmd['Id']} unbekannt")
        self._consume_inventory(cmd.get("ConsumedHeroInventory"))
        target["BoostId"] = sku["ItemId"]
        # Laufzeit als Expirable; ohne den Eintrag verwirft der Client den
        # Boost beim Laden (Felder: Client-Leser DefenseIngredientBoostExpirable)
        seconds = castles.duration(castles.boost_spec(sku["ItemId"])["Duration"])
        now = datetime.datetime.now(datetime.UTC)
        self.account.setdefault("Expirables", []).append({
            "$type": T("DefenseIngredientBoostExpirable"),
            "Id": cmd.get("ExpirableId") or NO_EXPIRABLE,
            "ExpirableType": "DefenseIngredientBoost",
            "DueDate": (now + datetime.timedelta(seconds=seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "DurationSeconds": seconds, "IsPaused": False,
            "BoostId": sku["ItemId"], "DefenseIngredientId": cmd["Id"],
            "DefenseItemType": cmd.get("ItemType", 5)})
        self.log(f"    [state] {key} {cmd['Id']} geboostet (Boost {sku['ItemId']})")

    def _cmd_AddCastleRoomCommand(self, cmd):
        """Raum kaufen (SKU ItemType 12, z. B. 10469 "Linear shrine 1",
        1000 Gold) oder ohne SkuCode aus InventoryRooms nehmen und an X/Y in
        den Entwurf setzen."""
        build = self.account["BuildInfo"]
        if not cmd.get("SkuCode"):
            inv = next((x for x in build.get("InventoryRooms") or []
                        if x["SpecContainerId"] == cmd["SpecContainerId"]
                        and x["AvailableCount"] > 0), None)
            if inv is None:
                raise ValueError(f"Raum {cmd['SpecContainerId']} nicht vorrätig")
            inv["AvailableCount"] -= 1
            self._place_room(cmd)
            return
        sku = self.data.skus[cmd["SkuCode"]]
        if sku["ItemType"] != 12 or sku["ItemId"] != cmd["SpecContainerId"]:
            raise ValueError(f"SKU {cmd['SkuCode']} passt nicht zu Raum "
                             f"{cmd['SpecContainerId']}")
        price = sku.get("Price") or {}
        field = CURRENCY[price["CurrencyType"]] if price else None
        wallet = self.account["Wallet"]
        if field and wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        self._consume_limited(sku)
        if field:
            wallet[field] -= price["Amount"]
        self._place_room(cmd)
        self.log(f"    [state] Raum gekauft: -{price.get('Amount', 0)} {field}")

    def _place_room(self, cmd):
        build = self.account["BuildInfo"]
        build["Draft"]["Rooms"].append({
            "$type": T("CastleRoom"), "X": cmd.get("X", 0), "Y": cmd.get("Y", 0),
            "Orientation": cmd.get("Orientation", 0), "Id": cmd["Id"], "SpecContainerId": cmd["SpecContainerId"]})
        build["RoomNextIndex"] = max(build.get("RoomNextIndex", 0), cmd["Id"] + 1)
        self.log(f"    [state] Raum {cmd['SpecContainerId']} (Id {cmd['Id']}) an "
                 f"{cmd.get('X')},{cmd.get('Y')} gesetzt")

    def _cmd_RemoveCastleRoomCommand(self, cmd):
        """Raum ins Inventar (InventoryRooms.AvailableCount +1). Inhalt, den
        der Client nicht vorher einzeln entfernt hat, geht mit ins Inventar."""
        build = self.account["BuildInfo"]
        room = self._room(cmd["Id"])
        for key, handler in (("Creatures", self._cmd_RemoveCastleCreatureCommand),
                             ("Traps", self._cmd_RemoveCastleTrapCommand),
                             ("Decorations", self._cmd_RemoveCastleDecorationCommand)):
            for x in list(room.get(key) or []):
                handler({"Id": x["Id"]})
        if room.get("Buildings"):
            raise ValueError(f"Raum {cmd['Id']} enthält noch Gebäude")
        build["Draft"]["Rooms"].remove(room)
        spec = room["SpecContainerId"]
        inv = next((x for x in build.setdefault("InventoryRooms", [])
                    if x["SpecContainerId"] == spec), None)
        if inv is None:
            inv = {"$type": T("InventoryRoom"), "AvailableCount": 0,
                   "Level": 1, "SpecContainerId": spec}
            build["InventoryRooms"].append(inv)
        inv["AvailableCount"] += 1
        self.log(f"    [state] Raum {spec} (Id {cmd['Id']}) ins Inventar")

    def _cmd_UpdateCastleRoomCommand(self, cmd):
        """Raum verschieben/drehen (X/Y, Orientation)."""
        room = self._room(cmd["Id"])
        room["X"], room["Y"] = cmd.get("X", 0), cmd.get("Y", 0)
        room["Orientation"] = cmd.get("Orientation", 0)
        self.log(f"    [state] Raum {cmd['Id']} nach {room['X']},{room['Y']} "
                 f"(Ausrichtung {room['Orientation']}) verschoben")

    def _cmd_AddBuildingCommand(self, cmd):
        """Gebäude kaufen (SKU, ItemType 10) und platzieren, z. B. Goldmine
        im Bau-Tutorial (Assignment 50 wartet auf BuildingUpgradeCompleted).
        Ohne Bauzeit: gleich Rang 1. Gold-/Lebenskraft-Minen bekommen einen
        MineStatus mit vollem Speicher, damit das Tutorial-Ernten klappt; die
        Bling-Mine startet leer wie im Original (MineManager.AddMineStatus),
        sonst brächte jede neue Mine einen vollen Speicher Blings."""
        sku = self.data.skus[cmd["SkuCode"]]
        if sku["ItemType"] != 10 or sku["ItemId"] != cmd["SpecContainerId"]:
            raise ValueError(f"SKU {cmd['SkuCode']} passt nicht zu Gebäude "
                             f"{cmd['SpecContainerId']}")
        build = self.account["BuildInfo"]
        room = next((r for r in build["Draft"]["Rooms"] if r["Id"] == cmd["RoomId"]), None)
        if room is None:
            raise KeyError(f"Raum {cmd['RoomId']} unbekannt")
        # Instanzen begrenzt (Bling-/Material-Mine bis zu 3), der Client
        # prüft das selbst, Packs und Shop-SKUs zählen gemeinsam
        allowed = castles.building_instances(cmd["SpecContainerId"])
        count = sum(b["SpecContainerId"] == cmd["SpecContainerId"]
                    for r in build["Draft"]["Rooms"] for b in r.get("Buildings") or [])
        if allowed and (count >= len(allowed)
                        or build.get("CastleHeartRank", 1) < allowed[count]):
            raise ValueError(f"Gebäude {cmd['SpecContainerId']}: Instanz "
                             f"{count + 1} nicht erlaubt")
        # Pack-SKUs (z. B. 15722 "Materials Mine - Pack purchase") ohne Preis
        price = balance.sku_price(sku) or {"CurrencyType": 2, "Amount": 0}
        field = CURRENCY[price["CurrencyType"]]
        wallet = self.account["Wallet"]
        if wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        self._consume_limited(sku)
        wallet[field] -= price["Amount"]
        bid = cmd["Id"]
        room.setdefault("Buildings", []).append({
            "$type": T("CastleBuilding"), "Rank": 1, "RoomZoneId": cmd.get("RoomZoneId", 0),
            "X": cmd.get("X", 0), "Y": cmd.get("Y", 0),
            "Orientation": cmd.get("Orientation", 0), "Id": bid,
            "SpecContainerId": cmd["SpecContainerId"]})
        build["BuildingNextIndex"] = max(build.get("BuildingNextIndex", 0), bid + 1)
        mine = castles.mine_rank(cmd["SpecContainerId"], 1)
        if mine:
            build.setdefault("MineStatuses", {})[str(bid)] = {
                "$type": T("MineStatus"),
                "CumulatedAmount": 0 if cmd["SpecContainerId"] == balance.BLING_MINE else mine["Capacity"],
                "Productionlevel": mine.get("InitialLevel", 1), "CumulatedHeroItems": [],
                "IsActive": True, "IsDestroyed": False,
                "ProductionExpirableId": "000000000000000000000000",
                "ShieldExpirableId": "000000000000000000000000"}
        self._invalidate_rollback()
        self.events.append(("BuildingUpgradeStarted",
                            {"BuildingId": bid, "ExpirableId": NO_EXPIRABLE}))
        self.events.append(("BuildingUpgradeCompleted", {"BuildingId": bid, "NewRank": 1}))
        if mine and mine.get("LevelProductionPacks"):  # Material-O-Matic: Paket und Timer
            self.events += self.advance_material_mines()
        self.log(f"    [state] Gebäude {cmd['SpecContainerId']} (Id {bid}) gebaut, "
                 f"-{price['Amount']} {field}")

    STORAGE_FIELDS: ClassVar[dict[int, str]] = {3: "InGameCoin", 4: "LifeForce"}  # GoldStorage, LifeForceStorage

    def _cmd_UpgradeBuildingCommand(self, cmd):
        """Gebäude ausbauen (SKU ItemType 10, ItemCount = Zielrang), ohne
        Bauzeit: gleich BuildingUpgradeCompleted. Burgherz (1) setzt
        CastleHeartRank, Burg-Level und MaxConstructionPoints, Lager (3, 4)
        die Kapazität im Wallet, Minen den Speicher."""
        sku = self.data.skus[cmd["SkuCode"]]
        build = self.account["BuildInfo"]
        b = next((x for r in build["Draft"]["Rooms"] for x in r.get("Buildings") or []
                  if x["Id"] == cmd["Id"]), None)
        if b is None:
            raise KeyError(f"Gebäude {cmd['Id']} unbekannt")
        rank = cmd["BuildingUpgradeRank"]
        if (sku["ItemType"] != 10 or sku["ItemId"] != b["SpecContainerId"]
                or sku.get("ItemCount") != rank or rank != b.get("Rank", 0) + 1):
            raise ValueError(f"SKU {cmd['SkuCode']} passt nicht zu Gebäude {cmd['Id']} "
                             f"Rang {b.get('Rank', 0)} -> {rank}")
        spec = castles.building_ranks(b["SpecContainerId"])[rank - 1]
        if (spec.get("BuildingRequirementSpecContainerId") == 1
                and build.get("CastleHeartRank", 1) < spec.get("BuildingRequirementRank", 0)):
            raise ValueError(f"Burgherz-Rang {spec['BuildingRequirementRank']} nötig")
        # Wie BuildingManager.ValidateBuildingRequirement: Burgherz-Rang N
        # erst ab Heldenlevel N (sonst zweimal ausbaubar, das Tutorial hängt)
        hero_level = max((h["Level"] for h in self.account["Heroes"]), default=0)
        if b["SpecContainerId"] == 1 and hero_level < rank:
            raise ValueError(f"Burgherz-Rang {rank} braucht Heldenlevel {rank}, "
                             f"höchstes ist {hero_level}")
        price = sku.get("Price") or {}
        field = CURRENCY[price["CurrencyType"]] if price else None
        wallet = self.account["Wallet"]
        if field and wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        if field:
            wallet[field] -= price["Amount"]
        self._set_building_rank(b, rank)
        self.log(f"    [state] Gebäude {b['SpecContainerId']} (Id {b['Id']}) auf Rang "
                 f"{rank}, -{price.get('Amount', 0)} {field}")
        if b["SpecContainerId"] == 1:
            self._unlock_buildings()

    RANK_FIELDS: ClassVar[dict[int, str]] = {
        1: "CastleHeartRank", 2: "WorkersCabinRank", 13: "ArchitectOfficeRank"}

    def _set_building_rank(self, b, rank):
        """Rang setzen samt abhängiger Werte; meldet BuildingUpgradeCompleted."""
        build, wallet = self.account["BuildInfo"], self.account["Wallet"]
        spec_id = b["SpecContainerId"]
        spec = castles.building_ranks(spec_id)[rank - 1]
        b["Rank"] = rank
        if spec_id in self.RANK_FIELDS:
            build[self.RANK_FIELDS[spec_id]] = rank
        level_up = capacity_event = None
        if spec_id == 1:
            # Burg-Level = Herz-Rang; der Client erfährt es nur über
            # CastleLevelUp (Tutorials warten darauf, z. B. Assignment 3000
            # "Potion brewery tutorial": CastleLevelUpAssignmentTrigger Level 3).
            # Erst nach BuildingUpgradeCompleted des Herzens melden.
            if rank > build.get("Level", 1):
                # AccountId muss passen, sonst verwirft der Client die
                # Meldung (Handler 0xd59170; Leser 0x8bb4f0)
                level_up = {"AccountId": self.account["AccountId"], "Level": rank,
                            "OldLevel": build.get("Level", 1)}
            build["Level"] = rank
            build.setdefault("CastleStats", {})["MaxConstructionPoints"] = spec[
                "MaxConstructionPoints"]
        elif spec_id in self.STORAGE_FIELDS:
            field = self.STORAGE_FIELDS[spec_id]
            wallet[field + "StorageCapacity"] = spec["Capacity"]
            # Sonst kennt der Client die neue Kapazität erst nach Relogin
            capacity_event = ("WalletCurrencyCapacityUpdated",
                              {"$type": T("WalletCapacityUpdatedNotification"),
                               "CurrencyType": {v: k for k, v in CURRENCY.items()}[field],
                               "Amount": spec["Capacity"]})
        # Started vor Completed: der Client merkt sich den Ausbau beim
        # Absenden als laufend und streicht ihn erst bei Started (Handler
        # 0xd09b30); sonst Assert "Building 1 upgrade is already started"
        # beim nächsten Ausbau. Ohne Bauzeit kein Expirable.
        self._invalidate_rollback()
        self.events.append(("BuildingUpgradeStarted",
                            {"BuildingId": b["Id"], "ExpirableId": NO_EXPIRABLE}))
        self.events.append(("BuildingUpgradeCompleted",
                            {"BuildingId": b["Id"], "NewRank": rank}))
        if level_up:
            self.events.append(("CastleLevelUp", level_up))
        if capacity_event:
            self.events.append(capacity_event)

    def _unlock_buildings(self):
        """Noch ungebaute Gebäude der Burg (Rang 0) bekommen Rang 1, sobald
        das Burgherz reicht: Rang 1 ist eine Gratis-SKU (ShopContext 6, z. B.
        10117 Architect's Office), der Client bietet keinen Bau an, sondern
        zeigt nur "Requires Castle Heart rank N"."""
        heart = self.account["BuildInfo"].get("CastleHeartRank", 1)
        for r in self.account["BuildInfo"]["Draft"]["Rooms"]:
            for b in r.get("Buildings") or []:
                ranks = castles.building_ranks(b["SpecContainerId"])
                if (not b.get("Rank") and ranks
                        and ranks[0].get("BuildingRequirementRank", 0) <= heart):
                    self._set_building_rank(b, 1)
                    self.log(f"    [state] Gebäude {b['SpecContainerId']} "
                             f"(Id {b['Id']}) freigeschaltet")

    def add_trophies(self, delta):
        """Kronen ändern (nicht unter 0); liefert den neuen Stand."""
        with self.lock:
            score = max(0, self.account.get("TrophyScore", 0) + delta)
            self.account["TrophyScore"] = score
            self._save()
        self.log(f"    [state] Kronen {delta:+d} -> {score}")
        return score

    def _add_corpses(self, defense, attacker_summary, entry):
        """Je Tod des Angreifers eine Heldenleiche (HeroCorpseSettings:
        höchstens MaxHeroCorpsesInCastle, die älteste fällt weg; je
        Angreifer nur eine pro HeroCorpseCooldownPerAttacker = 1 Tag).
        Das Material wird beim Tod gewürfelt. Liefert die neuen Leichen."""
        settings = item_drops._load("GeneralSettings", "HeroCorpseSettings.json")
        build = self.account["BuildInfo"]
        corpses = build.setdefault("HeroCorpses", [])
        meta = self.account.setdefault(self.CORPSES, {})
        now = datetime.datetime.now(datetime.UTC)
        attacker = attacker_summary.get("Id")
        if any(m["Attacker"] == attacker and now - datetime.datetime.fromisoformat(
                m["Died"]) < datetime.timedelta(days=1) for m in meta.values()):
            return []
        castle = castles.player_castle(self.account) or {}
        added = []
        for x, z in defense.get("death_spots", [])[:1]:
            cid = max([0] + [c["EntityHarvestId"] for c in corpses]) + 1
            materials = item_drops.corpse_materials(castle)
            # Ein Betrag ist Pflicht (sonst Assert "Invalid CurencyAmount" in
            # HeroCorpseHarvestingComponent); HeroCorpseSettings kennt keine
            # Währung mehr, also 0 Lebenskraft
            corpse = {"$type": T("CastleHeroCorpse"), "Amounts": [CORPSE_AMOUNT],
                      "ElapsedTimeSinceDeath": 0.0, "EntityHarvestId": cid,
                      "GlobalX": x, "GlobalZ": z,
                      "HeroDisplayName": attacker_summary.get("DisplayName") or "",
                      "HeroLevel": entry.get("Level", 1),
                      "HeroSpecContainerId": entry.get("HeroSpecContainerId", 0),
                      # ohne Inhalt lässt der Client die Leiche nicht anklicken
                      "Items": [{"$type": T("HeroConsumableItem"), "TemplateId": tid,
                                 "StackCount": n} for tid, n in materials]}
            corpses.append(corpse)
            meta[str(cid)] = {"Attacker": attacker, "Died": now.isoformat(),
                              "Materials": materials}
            added.append(copy.deepcopy(corpse))
        while len(corpses) > settings["MaxHeroCorpsesInCastle"]:
            meta.pop(str(corpses.pop(0)["EntityHarvestId"]), None)
        return added

    def corpse_ages(self, payload):
        """ElapsedTimeSinceDeath (Sekunden) der Heldenleichen im Account
        `payload` (Kopie für GetAccountInformation) aus dem Todeszeitpunkt."""
        meta = payload.get(self.CORPSES) or {}
        now = datetime.datetime.now(datetime.UTC)
        for c in (payload.get("BuildInfo") or {}).get("HeroCorpses") or []:
            died = (meta.get(str(c["EntityHarvestId"])) or {}).get("Died")
            if died:
                c["ElapsedTimeSinceDeath"] = float(int(
                    (now - datetime.datetime.fromisoformat(died)).total_seconds()))

    def record_defense(self, defense, attacker_summary):
        """Angriff auf die eigene Burg (attack._defense): geplünderte Minen
        leeren, Schatzkammer-Beute vom Wallet abziehen, Kronen, Angriffszähler, Eintrag vorn ins Verteidigungslog
        (höchstens BattleLogSettings.MaximumDefendLog), Heldenleichen.
        Liefert den Eintrag, den neuen Kronenstand (None: unverändert), die
        neuen Leichen, die Wallet-Änderung (Schatzkammer) und die
        Minen-Meldungen."""
        with self.lock:
            self.produce_mines()
            build = self.account["BuildInfo"]
            mines = build.get("MineStatuses") or {}
            notes = self._pillage_mines(mines, defense)
            # Schatzkammer: der Angreifer nimmt einen Anteil des Wallets mit
            # (höchstens, was noch da ist)
            wallet = self.account["Wallet"]
            deltas = {}
            for field, n in (defense.get("treasure") or {}).items():
                take = min(n, wallet.get(field, 0))
                if take:
                    wallet[field] -= take
                    deltas[field] = -take
            build["TotalAttackCount"] = build.get("TotalAttackCount", 0) + 1
            stats = build.setdefault("CastleStats", {"$type": T("CastleStats")})
            stats["AttackCount"] = stats.get("AttackCount", 0) + 1
            stats["HeroesKilled"] = stats.get("HeroesKilled", 0) + defense["deaths"]
            if defense["won"]:
                build["TotalSuccessfulAttackCount"] = build.get("TotalSuccessfulAttackCount", 0) + 1
                stats["SuccessfulAttackCount"] = stats.get("SuccessfulAttackCount", 0) + 1
            entry: dict[str, Any] = dict(defense["entry"], AttackerAccountSummary=attacker_summary,
                         AttackerCastleLevel=attacker_summary.get("CastleLevel", 1),
                         AttackStartDateTime=datetime.datetime.now(
                             datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
            # inzwischen ausgegeben: nur das tatsächlich Verlorene eintragen
            for field, key in (("InGameCoin", "StolenIGC"), ("LifeForce", "StolenLifeForce")):
                short = (defense.get("treasure") or {}).get(field, 0) + deltas.get(field, 0)
                if short:
                    entry[key] = dict(entry[key], Amount=entry[key]["Amount"] - short)
            log = self.account.setdefault("DefendLog", {"$type": T("DefendLog")})
            log["DefendLogEntries"] = ([entry] + (log.get("DefendLogEntries") or []))[
                :MAXIMUM_DEFEND_LOG]
            score = None
            if defense["trophies"]:
                score = max(0, self.account.get("TrophyScore", 0) + defense["trophies"])
                self.account["TrophyScore"] = score
            corpses = self._add_corpses(defense, attacker_summary, entry)
            self._save()
        self.log(f"    [state] Burg angegriffen von {attacker_summary.get('DisplayName')}: "
                 f"{'verloren' if defense['won'] else 'gehalten'}, "
                 f"-{entry['StolenIGC']['Amount']} Gold, "
                 f"-{entry['StolenLifeForce']['Amount']} Lebenskraft, "
                 f"Kronen {defense['trophies']:+d}")
        return copy.deepcopy(entry), score, corpses, deltas, notes

    def _pillage_mines(self, mines, defense):
        """Geplünderte Minen wie MineManager.ComputePillagedMinesLoss im
        Original: Verlust abziehen (höchstens der Füllstand), Zerstörung
        merken (bleibt bis RestoreMinesBuildingCommand), neu zerstörte Mine
        ohne laufenden Minenschild bekommt einen (MineShieldExpirable,
        MineLossPonderationTable); je Mine MinePillaged an den Besitzer."""
        now = datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        shields = {e["Id"] for e in self.account.get("Expirables") or []
                   if e.get("ExpirableType") == "MineShield" and e["DueDate"] > stamp}
        notes = []
        for bid, n in defense["mines"].items():
            status = mines.get(str(bid))
            if status is None:
                continue
            shielded = status.get("ShieldExpirableId") in shields
            amount = 0 if shielded else min(n, status["CumulatedAmount"])
            status["CumulatedAmount"] -= amount
            newly = bid in defense["destroyed"] and not status.get("IsDestroyed")
            status["IsDestroyed"] = bool(status.get("IsDestroyed")) or bid in defense["destroyed"]
            if bid in defense["destroyed"] and not shielded:
                notes += self._mine_shield(bid, status, defense["mine_shield"], now)
            notes.append(("MinePillaged", {
                "CastleBuildingId": bid, "StolenAmount": amount, "Destroyed": newly,
                "ShieldExpirableId": status["ShieldExpirableId"] if newly else NO_EXPIRABLE}))
        notes += self._pillage_material_mines(mines, defense, shields, now)
        return notes

    def _mine_shield(self, bid, status, seconds, now):
        """Minenschild nach der Zerstörung (MineShieldExpirable, Dauer aus
        MineLossPonderationTable); [ExpirableAdded] oder []."""
        if seconds <= 0:
            return []
        e = {"$type": T("MineShieldExpirable"), "Id": f"{random.getrandbits(96):024x}",
             "ExpirableType": "MineShield", "BuildingId": bid,
             "DueDate": (now + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.account.setdefault("Expirables", []).append(e)
        status["ShieldExpirableId"] = e["Id"]
        return [("ExpirableAdded", {"Expirable": dict(e)})]

    def _pillage_material_mines(self, mines, defense, shields, now):
        """Material-O-Matics: gestohlene Zutaten aus dem Paket nehmen (die
        Stufe bleibt), Zerstörung merken; der Besitzer bekommt MinePillaged
        (StolenAmount zieht der Client selbst ab) und MineProductionCompleted
        mit ProductionAdded 0: nur darüber erneuert der Client die
        Zutatenliste (InventoryItemAdded), sonst zeigte er weiter das volle
        Paket (im Spiel geprüft)."""
        notes = self.advance_material_mines()
        specs = {bid: spec for bid, _, spec in self._material_mines()}
        lost = defense.get("material_mines") or {}
        destroyed = defense.get("material_destroyed") or set()
        for bid in set(lost) | destroyed:
            status = mines.get(str(bid))
            if status is None:
                continue
            pack = status.setdefault("CumulatedHeroItems", [])
            taken = 0
            shielded = status.get("ShieldExpirableId") in shields
            if not shielded:
                for item in lost.get(bid) or []:
                    match = next((i for i in pack if i["TemplateId"] == item["TemplateId"]), None)
                    if match is not None:
                        pack.remove(match)
                        taken += 1
            status["CumulatedAmount"] = len(pack)
            newly = bid in destroyed and not status.get("IsDestroyed")
            status["IsDestroyed"] = bool(status.get("IsDestroyed")) or bid in destroyed
            if bid in destroyed and not shielded:  # wie Gold-/Lebenskraftminen
                notes += self._mine_shield(bid, status, defense.get("mine_shield", 0), now)
            notes.append(("MinePillaged", {
                "CastleBuildingId": bid, "StolenAmount": taken, "Destroyed": newly,
                "ShieldExpirableId": status["ShieldExpirableId"] if newly else NO_EXPIRABLE}))
            if taken and str(bid) in specs:
                # "if you're attacked, the process slows down": die nächste
                # Periode füllt das Paket wieder auf, statt die Stufe zu heben;
                # auf der höchsten Stufe stand der Timer, er läuft wieder an
                if status.get("ProductionExpirableId", NO_EXPIRABLE) == NO_EXPIRABLE:
                    notes += self._start_material_production(
                        str(bid), status, specs[str(bid)],
                        datetime.datetime.now(datetime.UTC))
                notes.append(self._material_completed(str(bid), status, 0))
        return notes

    def expire_mine_shields(self, now):
        """Abgelaufene Minenschilde entfernen; liefert MineShieldExpired."""
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            gone = [e for e in self.account.get("Expirables") or []
                    if e.get("ExpirableType") == "MineShield" and e["DueDate"] <= stamp]
            if not gone:
                return []
            statuses = self.account["BuildInfo"].get("MineStatuses") or {}
            for e in gone:
                self.account["Expirables"].remove(e)
                status = statuses.get(str(e.get("BuildingId")))
                if status and status.get("ShieldExpirableId") == e["Id"]:
                    status["ShieldExpirableId"] = NO_EXPIRABLE
            self._save()
        return [("MineShieldExpired", {"ExpirableId": e["Id"], "CastleBuildingId": e.get("BuildingId")})
                for e in gone]

    def _cmd_RestoreMinesBuildingCommand(self, cmd):
        """Zerstörte Minen wieder aufbauen (kostenlos, wie
        MineManager.RestoreMines im Original)."""
        statuses = self.account["BuildInfo"].get("MineStatuses") or {}
        for bid in cmd.get("CastleBuildingIds") or []:
            if str(bid) in statuses:
                statuses[str(bid)]["IsDestroyed"] = False

    def use_revenge(self, attack_id):
        """Rache für den Verteidigungslog-Eintrag `attack_id` genommen:
        Revenge danach gesperrt. True, wenn der Eintrag sie noch erlaubte."""
        with self.lock:
            log = self.account.get("DefendLog") or {}
            for entry in log.get("DefendLogEntries") or []:
                if entry.get("AttackId") == attack_id and entry.get("RevengeEnabled"):
                    entry["RevengeEnabled"] = False
                    self._save()
                    return True
        return False

    def set_trophy_record(self, castle_id, record):
        """Kronen-Eintrag zu einer angegriffenen Burg (trophies.settle)
        speichern; der alte Sieg-Zeitpunkt (trophies.COOLDOWNS) ist darin
        aufgegangen."""
        with self.lock:
            self.account.setdefault(trophies.CASTLES, {})[str(castle_id)] = record
            (self.account.get(trophies.COOLDOWNS) or {}).pop(str(castle_id), None)
            self._save()

    def _init_league(self):
        """Neuer Account: Liga nach den Kronen, Saison = laufende (keine
        Abrechnung rückwirkend); siehe leagues.settle."""
        changed = False
        if not self.account.get("LeagueId"):
            self.account["LeagueId"], self.account["SubLeagueId"] = \
                trophies.league_of(self.account.get("TrophyScore", 0))
            changed = True
        for key, start in ((leagues.SEASON_START, leagues.season_start()),
                           (leagues.PERIOD_START, leagues.period_start())):
            if key not in self.account:
                self.account[key] = start
                changed = True
        return changed

    def settle_league(self, result):
        """Liga-Abrechnung (leagues.settle) übernehmen: Abrechnungsstände,
        neue Liga, Saisonergebnis fürs Saisonende-Fenster, Belohnungen,
        News. Liefert (Wallet-Deltas, Deko-SpecIds, News)."""
        wallet, decorations, news = {}, [], []
        with self.lock:
            self.account.update(result["Marks"])
            if result["League"]:
                self.account["LeagueId"], self.account["SubLeagueId"] = result["League"]
            if result["Previous"]:
                self.account[leagues.PREVIOUS_SEASON] = result["Previous"]
            for reward in result["Rewards"]:
                cash, decos = leagues.reward_parts(reward)
                for f, n in cash.items():
                    got = self._credit(f, n)
                    if f == "PremiumCash":
                        self._tag("leagues", got)
                    wallet[f] = wallet.get(f, 0) + got
                for spec in decos:
                    self._add_inventory_decoration(spec)
                decorations += decos
            news = [self._add_news(d) for d in result["News"]]
            self._save()
        self.log(f"    [state] Liga-Abrechnung: Liga {result['League']}, "
                 f"Saison {result['Previous']}, {wallet}, Deko {decorations}, "
                 f"{len(news)} News")
        return wallet, decorations, news

    def settle_competitions(self, result):
        """Wettbewerbs-Abrechnung (competitions.settle) übernehmen:
        Ids merken, Edelsteine gutschreiben, News. Liefert (Wallet, News)."""
        wallet = {}
        with self.lock:
            self.account[competitions.SETTLED] = (
                (self.account.get(competitions.SETTLED) or []) + result["Ids"])
            for reward in result["Rewards"]:
                for f, n in leagues.reward_parts(reward)[0].items():
                    got = self._credit(f, n)
                    if f == "PremiumCash":
                        self._tag("competitions", got)
                    wallet[f] = wallet.get(f, 0) + got
            news = [self._add_news(d) for d in result["News"]]
            self._save()
        self.log(f"    [state] Wettbewerbe {result['Ids']} abgerechnet: {wallet}")
        return wallet, news

    def mark_season_viewed(self):
        with self.lock:
            prev = self.account.get(leagues.PREVIOUS_SEASON)
            if prev and not prev.get("Viewed"):
                prev["Viewed"] = True
                self._save()

    def _add_news(self, data):
        """News an den Account hängen (neueste zuerst, höchstens
        NewsMaxCount); Id zeitlich aufsteigend wie eine ObjectId."""
        now = datetime.datetime.now(datetime.UTC)
        box = self.account.setdefault("News", {"$type": T("NewsResult"),
                                               "LastViewedNewsId": "", "News": []})
        news = {"$type": T("News"), "Id": f"{int(now.timestamp()):08x}{random.getrandbits(64):016x}",
                "Data": data, "IsUnread": True, "Priority": 1,
                "PublishDate": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        box["News"] = [news] + self._current_news(box, now)[:NEWS_MAX - 1]
        return news

    @staticmethod
    def _current_news(box, now):
        """News ohne die nach NewsTimeout abgelaufenen."""
        return [n for n in box.get("News") or []
                if now - datetime.datetime.fromisoformat(
                    n["PublishDate"]) < NEWS_TIMEOUT]

    def add_news(self, data):
        """News von außen (Freundes-News); liefert den Eintrag für NewsAdded."""
        with self.lock:
            news = self._add_news(data)
            self._save()
        return copy.deepcopy(news)

    def expire_news(self):
        """Beim Laden des Accounts: abgelaufene News entfernen."""
        box = self.account.get("News")
        if box:
            box["News"] = self._current_news(
                box, datetime.datetime.now(datetime.UTC))

    def _cmd_SetLastViewedNewsCommand(self, cmd):
        """News-Fenster geöffnet: alles bis zur Id gilt als gelesen."""
        box = self.account.get("News") or {}
        box["LastViewedNewsId"] = cmd.get("NewsId") or ""
        for n in box.get("News") or []:
            n["IsUnread"] = n["Id"] > box["LastViewedNewsId"]

    # Dauer der letzten Validierung in Sekunden (Par-Zeit der Burg für
    # Angreifer, CastleValidationDuration); nur serverseitig
    VALIDATION_DURATION = "ValidationDuration"

    # Räume (mit Inhalt) und Minenstatus der zuletzt validierten Burg, Ziel
    # von "Entwurf verwerfen" (Original: CastleValidBlueprint); nur serverseitig
    VALID_BLUEPRINT = "ValidBlueprint"

    # Räume der zuletzt validierten Burg: das, was Angreifer sehen
    # (castles.player_castle). Anders als VALID_BLUEPRINT bleibt es bei
    # Ausbau und Verkauf stehen; nur serverseitig
    PUBLISHED_ROOMS = "PublishedRooms"

    def validate_draft(self, seconds):
        """Burg testen bis zur Schatzkammer geschafft: Entwurf gilt als
        veröffentlicht, die gebrauchte Zeit wird die Par-Zeit. Der Stand wird
        Ziel für "Entwurf verwerfen" (CastleRollbackManager.OnCastleValidated)."""
        with self.lock:
            build = self.account["BuildInfo"]
            build["IsDraftPublished"] = True
            build["IsRollbackAvailable"] = True
            self.account[self.VALID_BLUEPRINT] = copy.deepcopy(
                {"Rooms": build["Draft"]["Rooms"],
                 "MineStatuses": build.get("MineStatuses", {})})
            self.account[self.VALIDATION_DURATION] = seconds
            self.account[self.PUBLISHED_ROOMS] = copy.deepcopy(build["Draft"]["Rooms"])
            # offenes Validierungs-Ticket ist damit erledigt; Preis zurück
            # (Verhalten des Originals unbekannt)
            out = self._cancel_delegation()
            self._save()
        self.log(f"    [state] Burg validiert ({seconds:.1f} s)")
        return out

    def _invalidate_rollback(self):
        """Gebäudeausbau und Verkauf aus dem Vorrat lassen sich nicht
        verwerfen (CastleRollbackManager: OnBuildingUpgradeStarted,
        OnDefenseIngredientItemSold, OnDefenseIngredientUpgrade)."""
        self.account["BuildInfo"]["IsRollbackAvailable"] = False
        self.account.pop(self.VALID_BLUEPRINT, None)

    # Platzierte Dinge im Entwurf -> Vorrat in BuildInfo
    ROLLBACK_INVENTORY = (("Creatures", "CreatureArchetypes"),
                          ("Traps", "TrapArchetypes"),
                          ("Decorations", "InventoryDecorations"),
                          (None, "InventoryRooms"))

    def _cmd_CastleDraftRollbackCommand(self, cmd):
        """Entwurf verwerfen: Räume der zuletzt validierten Burg zurück,
        Vorrat neu rechnen (je Art und SpecContainerId: Vorrat + im Entwurf
        platziert - in der validierten Burg platziert)
        (CastleRollbackManager.RollbackCastle, RecomputeInventoryAfterRollback).
        Der Client wartet im Ladebildschirm auf CastleDraftRollbackCompleted
        {BuildInfo} (Feld laut Client; der ältere Servercode schickte
        BuildInfoUpdated)."""
        build = self.account["BuildInfo"]
        valid = self.account.get(self.VALID_BLUEPRINT)
        if not build.get("IsRollbackAvailable") or valid is None:
            raise ValueError("kein validierter Stand zum Zurücksetzen")

        def placed(rooms, key):
            n = {}
            for r in rooms:
                for x in ([r] if key is None else r.get(key) or []):
                    n[x["SpecContainerId"]] = n.get(x["SpecContainerId"], 0) + 1
            return n

        rooms = build["Draft"]["Rooms"]
        counts = []
        for key, field in self.ROLLBACK_INVENTORY:
            now, then = placed(rooms, key), placed(valid["Rooms"], key)
            stock = {x["SpecContainerId"]: x["AvailableCount"] for x in build.get(field) or []}
            new = {spec: stock.get(spec, 0) + now.get(spec, 0) - then.get(spec, 0)
                   for spec in set(stock) | set(now) | set(then)}
            missing = [spec for spec, n in new.items() if n < 0]
            if missing:
                raise ValueError(f"{field}: {missing} nicht mehr vorhanden")
            counts.append((field, new))
        # erst nach allen Prüfungen ändern
        for field, new in counts:
            entries = build.setdefault(field, [])
            for spec, n in sorted(new.items()):
                e = next((x for x in entries if x["SpecContainerId"] == spec), None)
                if e is None and n == 0:
                    continue
                if e is None:
                    e = ({"$type": T("DefenseIngredientArchetype"), "Specialization": 0, "Tier": 1}
                         if field.endswith("Archetypes") else
                         {"$type": T(field[:-1]), "Level": 1})
                    e["SpecContainerId"] = spec
                    entries.append(e)
                e["AvailableCount"] = n
        build["Draft"]["Rooms"] = copy.deepcopy(valid["Rooms"])
        mines = build.setdefault("MineStatuses", {})
        ids = {str(b["Id"]) for r in build["Draft"]["Rooms"] for b in r.get("Buildings") or []}
        for bid in list(mines):
            if bid not in ids:
                del mines[bid]
        for bid, status in valid["MineStatuses"].items():
            if bid in ids and bid not in mines:
                mines[bid] = copy.deepcopy(status)
        # Boosts gelten nur für Kreaturen/Fallen, die es noch gibt
        alive = {(5 if key == "Creatures" else 6, x["Id"]): x
                 for r in build["Draft"]["Rooms"] for key in ("Creatures", "Traps")
                 for x in r.get(key) or []}
        boosts = set()
        keep = []
        for e in self.account.get("Expirables") or []:
            if e.get("ExpirableType") == "DefenseIngredientBoost":
                x = alive.get((e.get("DefenseItemType", 5), e.get("DefenseIngredientId")))
                if x is None or x.get("BoostId") != e.get("BoostId"):
                    continue
                boosts.add(id(x))
            keep.append(e)
        self.account["Expirables"] = keep
        for x in alive.values():
            if x.get("BoostId") and id(x) not in boosts:
                x["BoostId"] = 0
        build["IsDraftPublished"] = True
        self.events.append(("CastleDraftRollbackCompleted", {"BuildInfo": copy.deepcopy(build)}))
        self.log("    [state] Entwurf verworfen, validierte Burg wiederhergestellt")

    def expire_boosts(self, now=None):
        """Abgelaufene DefenseIngredientBoostExpirables entfernen und die
        BoostId an Kreatur/Falle zurücksetzen. Der Client zählt die Laufzeit
        selbst herunter; der Server räumt beim Laden des Accounts auf."""
        now = now or datetime.datetime.now(datetime.UTC)
        keep, gone = [], []
        for e in self.account.get("Expirables") or []:
            due = e.get("DueDate")
            if (e.get("ExpirableType") == "DefenseIngredientBoost" and due
                    and datetime.datetime.fromisoformat(due) <= now):
                gone.append(e)
            else:
                keep.append(e)
        if not gone:
            return
        self.account["Expirables"] = keep
        key = {5: "Creatures", 6: "Traps"}
        for e in gone:
            for r in self.account["BuildInfo"]["Draft"]["Rooms"]:
                for x in r.get(key.get(e.get("DefenseItemType"), "Creatures")) or []:
                    if x["Id"] == e["DefenseIngredientId"] and x.get("BoostId") == e["BoostId"]:
                        x["BoostId"] = 0
            self.log(f"    [state] Boost {e['BoostId']} an {e['DefenseIngredientId']} abgelaufen")
        self._save()

    def produce_mines(self, now=None):
        """Minenertrag seit der letzten Abrechnung: je volle ProductionPeriod
        ProductionValue (Goldmine Rang 1: 7 je 5 Minuten), gedeckelt auf
        Capacity. Angefangene Perioden bleiben stehen (Uhr rückt nur um
        ganze Perioden vor)."""
        now = time.time() if now is None else now
        clock = self.account.setdefault(self.MINE_CLOCK, {})
        ranks = {str(b["Id"]): b for r in self.account["BuildInfo"]["Draft"]["Rooms"]
                 for b in r.get("Buildings") or []}
        changed = False
        for bid, status in self.account["BuildInfo"].get("MineStatuses", {}).items():
            b = ranks.get(bid)
            spec = b and castles.mine_rank(b["SpecContainerId"], b.get("Rank", 0))
            # zerstörte Minen produzieren weiter (Original: ComputeMineProduction
            # prüft IsDestroyed nicht)
            if (not spec or not status.get("IsActive", True)
                    or spec.get("LevelProductionPacks")):  # Material-O-Matic: material_mines
                continue
            period = castles.duration(spec["ProductionPeriod"])
            since = clock.setdefault(bid, now)
            n = int((now - since) // period)
            if n <= 0:
                continue
            clock[bid] = since + n * period
            status["CumulatedAmount"] = min(spec["Capacity"], status["CumulatedAmount"]
                                            + n * spec["ProductionValue"])
            changed = True
        if changed:
            self._save()

    # -- Material-O-Matic -----------------------------------------------------
    # MineStatus (Felder des finalen Clients): CumulatedAmount (Zutaten im
    # Paket), CumulatedHeroItems (die Zutaten selbst, je eine
    # HeroConsumableItem), Productionlevel, ProductionExpirableId
    # (MineProductionExpirable bis zur nächsten Periode). Eine Periode füllt
    # das Paket (Capacity Basic-Zutaten), jede weitere ohne Abholen hebt die
    # Stufe; ab Stufe N haben RandomQualityQuantity Zutaten eine Qualität nach
    # QualityChance. Auf der höchsten Stufe läuft kein Timer mehr.

    def _material_mines(self):
        """[(Building-Id als str, MineStatus, Rang-Spec)] der Material-O-Matics."""
        statuses = self.account["BuildInfo"].get("MineStatuses") or {}
        out = []
        for r in self.account["BuildInfo"]["Draft"]["Rooms"]:
            for b in r.get("Buildings") or []:
                status = statuses.get(str(b["Id"]))
                if status is None:
                    continue
                spec = castles.mine_rank(b["SpecContainerId"], b.get("Rank", 0))
                if spec and spec.get("LevelProductionPacks"):
                    out.append((str(b["Id"]), status, spec))
        return out

    @staticmethod
    def _material_target(spec, level):
        """Anzahl Zutaten mit gewürfelter Qualität auf Stufe level."""
        return next((p.get("RandomQualityQuantity", 0) for p in spec["LevelProductionPacks"]
                     if p["Level"] == level), 0)

    def _start_material_production(self, bid, status, spec, start):
        """Neues MineProductionExpirable ab start (datetime); liefert
        ExpirableAdded oder [] (höchste Stufe erreicht)."""
        old = status.get("ProductionExpirableId")
        self.account["Expirables"] = [e for e in self.account.get("Expirables") or []
                                      if e["Id"] != old]
        top = max(p["Level"] for p in spec["LevelProductionPacks"])
        full = status["CumulatedAmount"] >= spec["Capacity"]
        if full and status["Productionlevel"] >= top:
            status["ProductionExpirableId"] = NO_EXPIRABLE
            return []
        seconds = castles.duration(spec["ProductionPeriod"])
        e = {"$type": T("MineProductionExpirable"), "Id": f"{random.getrandbits(96):024x}",
             "ExpirableType": "MineProduction", "BuildingId": int(bid),
             "ProductionValue": spec["ProductionValue"], "DurationSeconds": seconds,
             "DueDate": (start + datetime.timedelta(seconds=seconds))
             .strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.account.setdefault("Expirables", []).append(e)
        status["ProductionExpirableId"] = e["Id"]
        return [("ExpirableAdded", {"Expirable": dict(e)})]

    def _upgrade_material_items(self, status, spec):
        """Basic-Zutaten aufwerten, bis die Stufe erreicht ist."""
        items = status["CumulatedHeroItems"]
        basic = set(item_drops.material_templates("Basic"))
        rolled = sum(1 for i in items if i["TemplateId"] not in basic)
        for i in items:
            if rolled >= self._material_target(spec, status["Productionlevel"]):
                break
            if i["TemplateId"] in basic:
                i["TemplateId"] = item_drops.mine_material(spec["QualityChance"])
                rolled += 1

    def _material_completed(self, bid, status, added):
        return ("MineProductionCompleted", {
            "BuildingId": int(bid), "ProductionAdded": added,
            "ProductionLevel": status["Productionlevel"],
            "InventoryItemAdded": copy.deepcopy(status["CumulatedHeroItems"]),
            "NextProductionExpirableId": status["ProductionExpirableId"]})

    def advance_material_mines(self, now=None):
        """Fällige Perioden der Material-O-Matics abrechnen (auch ältere
        Stände ohne Timer/Zutaten nachziehen); liefert die Meldungen
        (ExpirableAdded, MineProductionCompleted)."""
        now = now or datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        notes = []
        for bid, status, spec in self._material_mines():
            status.setdefault("Productionlevel", spec.get("InitialLevel", 1))
            items = status.setdefault("CumulatedHeroItems", [])
            changed, added = False, 0
            if status["CumulatedAmount"] and not items:  # Stand von vor den Zutaten
                items += [item_drops.material_item("Basic")
                          for _ in range(status["CumulatedAmount"])]
                self._upgrade_material_items(status, spec)
                changed = True
            exp = {e["Id"]: e for e in self.account.get("Expirables") or []}
            e = exp.get(status.get("ProductionExpirableId"))
            if e is None:
                notes += self._start_material_production(bid, status, spec, now)
                changed = changed or bool(notes)
            while e is not None and e["DueDate"] <= stamp:
                if status["CumulatedAmount"] < spec["Capacity"]:
                    n = spec["Capacity"] - status["CumulatedAmount"]
                    items += [item_drops.material_item("Basic") for _ in range(n)]
                    status["CumulatedAmount"] += n
                    added += n
                else:
                    top = max(p["Level"] for p in spec["LevelProductionPacks"])
                    status["Productionlevel"] = min(status["Productionlevel"] + 1, top)
                self._upgrade_material_items(status, spec)
                due = datetime.datetime.strptime(e["DueDate"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.UTC)
                notes += self._start_material_production(bid, status, spec, due)
                e = next((x for x in self.account["Expirables"]
                          if x["Id"] == status["ProductionExpirableId"]), None)
                changed = True
            if changed:
                notes.append(self._material_completed(bid, status, added))
        if notes:
            self._save()
        return notes

    def material_mines(self, now):
        """advance_material_mines unter der Sperre (Aufruf je Anfrage)."""
        with self.lock:
            return self.advance_material_mines(now)

    def _harvest_material_mine(self, cmd, bid, status, spec):
        """Material-O-Matic abholen: die Zutaten des Pakets ins Inventar
        (HarvestedHeroItems), zurück auf InitialLevel, neue Periode
        (MineProductionStarted)."""
        self.events += self.advance_material_mines()
        items = status.get("CumulatedHeroItems") or []
        if not items:
            raise ValueError(f"Material-O-Matic {bid} ist leer")
        level = status["Productionlevel"]
        status.update(CumulatedAmount=0, CumulatedHeroItems=[],
                      Productionlevel=spec.get("InitialLevel", 1))
        added = self._start_material_production(
            bid, status, spec, datetime.datetime.now(datetime.UTC))
        occupied = self._occupied()
        self.events.append(("HarvestingCompleted", {"Result": {
            "$type": T("HarvestResult"), "EntityHarvestId": cmd["MineBuildingId"],
            "EntityHarvestType": "CraftingMaterialMine",
            # keine Währung: Amount > 0 lässt den Client Währung "0" fallen
            # (Assert BaseHarvestingComponent::DropHarvestCurrencyLoot, im Spiel geprüft)
            "HarvestedAmount": {"$type": T("CurrencyAmount"), "Amount": 0,
                                "CurrencyType": cmd.get("CurrencyType", 0)},
            "HarvestedHeroItems": copy.deepcopy(items), "RemainingTime": 0,
            "ResultCode": "Success"}}))
        self.events += added
        self.events.append(("MineProductionStarted", {
            "BuildingId": int(bid), "ProductionLevel": status["Productionlevel"],
            "NextProductionExpirableId": status["ProductionExpirableId"]}))
        gained = collections.Counter(i["TemplateId"] for i in items)
        self.events += self._inventory_events(
            self._add_reward_items([("material", tid, n) for tid, n in gained.items()]),
            occupied)
        self.log(f"    [state] Material-O-Matic {bid} Stufe {level} geerntet: {dict(gained)}")

    def _cmd_HarvestMineBuildingCommand(self, cmd):
        """Mine ernten: gesammelter Ertrag ins Lager (gedeckelt). Der Client
        zeigt den Ertrag erst nach WalletUpdated an (ohne verschwand das
        Gold, im Spiel geprüft)."""
        self.produce_mines()
        status = self.account["BuildInfo"].get("MineStatuses", {}).get(
            str(cmd["MineBuildingId"]))
        if status is None:
            raise KeyError(f"Mine {cmd['MineBuildingId']} unbekannt")
        b = next((b for r in self.account["BuildInfo"]["Draft"]["Rooms"]
                  for b in r.get("Buildings") or [] if b["Id"] == cmd["MineBuildingId"]), None)
        spec = b and castles.mine_rank(b["SpecContainerId"], b.get("Rank", 0))
        if spec and spec.get("LevelProductionPacks"):
            self._harvest_material_mine(cmd, str(cmd["MineBuildingId"]), status, spec)
            return
        field = CURRENCY[cmd.get("CurrencyType", 2)]
        got = self._credit(field, status["CumulatedAmount"])
        status["CumulatedAmount"] = 0
        if field == "InGameCoin":
            achievements.add_gold(self.account, got)
        elif field == "PremiumCash":
            self._tag("mine", got)
        # ohne HarvestingCompleted bleibt der Client bei "Collecting" stehen;
        # Enums des HarvestResult liest er als Namen
        self.events.append(("HarvestingCompleted", {"Result": {
            "$type": T("HarvestResult"), "EntityHarvestId": cmd["MineBuildingId"],
            # PremiumCashMine (4) kennt erst der finale Client (entityHarvestType.js),
            # nicht die alte DLL; mit LifeForceMine ließ sich die Bling-Mine nicht ernten
            "EntityHarvestType": {"InGameCoin": "GoldMine", "LifeForce": "LifeForceMine",
                                  "PremiumCash": "PremiumCashMine"}[field],
            "HarvestedAmount": {"$type": T("CurrencyAmount"), "Amount": got,
                                "CurrencyType": cmd.get("CurrencyType", 2)},
            "HarvestedHeroItems": [], "RemainingTime": 0, "ResultCode": "Success"}}))
        if got:
            self.events.append(("WalletUpdated", {"UpdateTag": 0, "Amounts": [{
                "$type": T("CurrencyAmount"), "Amount": got,
                "CurrencyType": cmd.get("CurrencyType", 2)}]}))
        self.log(f"    [state] Mine {cmd['MineBuildingId']} geerntet: +{got} {field}")

    def _cmd_HarvestHeroCorpseCommand(self, cmd):
        """Heldenleiche ernten: das beim Tod gewürfelte Material ins
        Inventar, Leiche weg. HarvestingCompleted wie bei den Minen."""
        cid = cmd["HeroCorpseId"]
        corpses = self.account["BuildInfo"].get("HeroCorpses") or []
        if not any(c["EntityHarvestId"] == cid for c in corpses):
            raise KeyError(f"Heldenleiche {cid} unbekannt")
        self.account["BuildInfo"]["HeroCorpses"] = [
            c for c in corpses if c["EntityHarvestId"] != cid]
        meta = (self.account.get(self.CORPSES) or {}).pop(str(cid), None) or {}
        items = [("consumable", tid, n) for tid, n in meta.get("Materials") or []]
        occupied = self._occupied()
        self.events.append(("HarvestingCompleted", {"Result": {
            "$type": T("HarvestResult"), "EntityHarvestId": cid,
            "EntityHarvestType": "HeroCorpse",
            "HarvestedAmount": CORPSE_AMOUNT,
            "HarvestedHeroItems": [], "RemainingTime": 0, "ResultCode": "Success"}}))
        self.events += self._inventory_events(self._add_reward_items(items), occupied)
        self.log(f"    [state] Heldenleiche {cid} geerntet: {items}")

    def _cmd_SelectHeroCommand(self, cmd):
        """Heldenwechsel; der Client wartet auf HeroSelected (Felder nicht
        belegt, HeroId wie im Command)."""
        hero = self._hero(cmd["HeroId"])
        self.account["SelectedHeroId"] = cmd["HeroId"]
        self.events.append(("HeroSelected", {
            "HeroId": cmd["HeroId"], "HeroSpecContainerId": cmd["HeroId"],
            "Hero": copy.deepcopy(hero)}))
        self.log(f"    [state] Held {cmd['HeroId']} ausgewählt")

    def _buy_hero(self, sku):
        """Held per SKU (ItemType 1, ItemId = Helden-Spec, z. B. 950 Gems)."""
        spec_id = sku["ItemId"]
        hero = next((h for h in self.account["Heroes"]
                     if h["HeroSpecContainerId"] == spec_id), None)
        expired = next((h for h in self.account.get(self.EXPIRED_TRIALS) or []
                        if h["HeroSpecContainerId"] == spec_id), None)
        if hero is not None and hero.get("ExpirationDate") is None:
            raise ValueError(f"Held {spec_id} schon vorhanden")
        price = sku["Price"]
        field = CURRENCY[price["CurrencyType"]]
        amount = price["Amount"]
        if hero is not None:  # Probe-Held: Rabatt der erreichten Ziele
            amount = max(0, amount - free_trials.price_reduction(hero, field))
        wallet = self.account["Wallet"]
        if wallet[field] < amount:
            raise ValueError(f"zu wenig {field}")
        wallet[field] -= amount
        if hero is None and expired is not None:  # abgelaufener Probe-Held
            self.account[self.EXPIRED_TRIALS].remove(expired)
            hero = expired
            self.account["Heroes"].append(hero)
        if hero is None:
            hero = self._new_hero(spec_id)
            self.account["Heroes"].append(hero)
        else:  # behält Level und Ausrüstung aus dem Trial
            hero.pop("ExpirationDate", None)
            hero.pop("PriceReductionIds", None)
        self.spells_unlocked += self._unlock_spells()
        # Ohne Meldung zeigt der Client den gekauften Helden nicht an.
        # Felder: HeroUnlockedNotification hat keine eigenen Strings im
        # Binary, nutzt also geteilte Namen -- "Hero" (Annahme)
        self.events.append(("HeroUnlocked", {
            "Hero": copy.deepcopy(hero), "HeroSpecContainerId": spec_id}))
        self.log(f"    [state] Held {spec_id} gekauft, -{amount} {field}")

    def _cmd_BuyHeroItemCommand(self, cmd):
        sku = self.data.skus[cmd["SkuCode"]]
        price = sku["Price"]
        field = CURRENCY[price["CurrencyType"]]
        wallet = self.account["Wallet"]
        if wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        if sku["ItemType"] != 3:  # ItemType.HeroEquipmentItem
            raise ValueError(f"SKU-ItemType {sku['ItemType']} nicht unterstützt")
        self._consume_inventory(cmd.get("ConsumedHeroInventory"))  # "Item Crafting"
        self._consume_limited(sku)
        wallet[field] -= price["Amount"]
        # Level des gewählten Helden innerhalb der Template-Spanne, so zeigt
        # und legt der Client es an; auf LevelMax (z. B. Stab 3-7 -> 7) lehnte
        # der Server das Anlegen für Helden darunter ab, nach dem Neuladen
        # lag das Teil untragbar in den Taschen und die Hand war leer
        t = self.data.templates[sku["ItemId"]]
        level = None if self.data.is_cosmetic(t) else max(
            t.get("LevelMin", 1), min(t.get("LevelMax", t.get("LevelMin", 1)),
                                      self._hero(self.account["SelectedHeroId"])["Level"]))
        self._put_slot(cmd["SlotIndex"], self._item_from_template(sku["ItemId"], level))
        self.log(f"    [state] gekauft: SKU {cmd['SkuCode']} -> Template "
                 f"{sku['ItemId']} in Slot {cmd['SlotIndex']}, "
                 f"-{price['Amount']} {field}")

    def _cmd_BuyConsumableCommand(self, cmd):
        """Tränke per SKU (ItemType 4, ItemId = Consumable-Template,
        ItemCount Stück); der Client gibt die Aufteilung auf Inventar-Slots
        vor (SlotIndexes {Slot: Anzahl}), auch auf vorhandene Stapel."""
        sku = self.data.skus[cmd["SkuCode"]]
        if sku["ItemType"] != 4:  # ItemType.Consumable
            raise ValueError(f"SKU-ItemType {sku['ItemType']} nicht unterstützt")
        price = sku["Price"]
        field = CURRENCY[price["CurrencyType"]]
        wallet = self.account["Wallet"]
        if wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        self._consume_limited(sku)
        wallet[field] -= price["Amount"]
        for index, n in cmd["SlotIndexes"].items():
            index = int(index)
            slot = next((s for s in self._slots() if s["SlotIndex"] == index), None)
            if slot is not None and slot["Item"].get("TemplateId") == sku["ItemId"]:
                slot["StackCount"] += n
            else:
                self._put_slot(index, {"$type": T("HeroConsumableItem"),
                                       "TemplateId": sku["ItemId"]}, n)
        self.log(f"    [state] gekauft: {sku['ItemCount']}x Trank {sku['ItemId']} "
                 f"-> Slots {cmd['SlotIndexes']}, -{price['Amount']} {field}")

    def _cmd_HeroEquipConsumableCommand(self, cmd):
        """Trank in den Gürtel des Helden (EquippedSlotIndex); Anzahl = Vorrat
        im Inventar (der Gürtel verweist auf die Inventarstapel)."""
        hero = self._hero(cmd["HeroId"])
        count = sum(s["StackCount"] for s in self._slots()
                    if s["Item"]["$type"] == T("HeroConsumableItem")
                    and s["Item"].get("TemplateId") == cmd["TemplateId"])
        slots = [x for x in hero.get("EquippedConsumables") or []
                 if x["SlotIndex"] != cmd["EquippedSlotIndex"]]
        slots.append({"$type": T("HeroConsumableSlot"),
                      "SlotIndex": cmd["EquippedSlotIndex"],
                      "StackCount": count, "TemplateId": cmd["TemplateId"]})
        hero["EquippedConsumables"] = sorted(slots, key=lambda x: x["SlotIndex"])
        self.log(f"    [state] Held {cmd['HeroId']}: Trank {cmd['TemplateId']} "
                 f"in Gürtel-Slot {cmd['EquippedSlotIndex']}")

    def _cmd_HeroEquipmentEquipCommand(self, cmd):
        hero = self._hero(cmd["HeroId"])
        slot = CLIENT_SLOTS[cmd["DestinationSlot"]]
        src = next((x for x in self._slots()
                    if x["SlotIndex"] == cmd["SourceSlotId"]), None)
        if src is None:
            raise KeyError(f"Inventar-Slot {cmd['SourceSlotId']} leer")
        # InventoryManager.IsEquipmentValid: Heldenlevel >= ItemLevel und
        # Gegenstandstyp in HeroItemTypes der Heldenklasse
        item = src["Item"]
        t = self.data.templates.get(item.get("TemplateId"), {})
        types = {x["HeroItemTypeId"] for x in
                 _equipment_spec(cmd["HeroId"]).get("HeroItemTypes") or []}
        if hero["Level"] < item.get("ItemLevel", 0) or t.get("HeroItemTypeId") not in types:
            raise ValueError(f"Item {item.get('TemplateId')} (Level "
                             f"{item.get('ItemLevel')}) für Held {cmd['HeroId']} "
                             "nicht tragbar")
        self._take_slot(cmd["SourceSlotId"])
        old = hero["Equipment"].get(slot)
        hero["Equipment"][slot] = item
        # Tausch: bisheriges Item landet im freigewordenen Slot; Standard-
        # Ausrüstung verfällt wie im Original (InventoryManager.EquipItem),
        # in der Tasche zeigte der Client sie als nicht tragbar
        if old and old.get("TemplateId") != self._default_equipment(
                cmd["HeroId"]).get(slot):
            self._put_slot(cmd["SourceSlotId"], old)
        self.log(f"    [state] Held {cmd['HeroId']}: {slot} <- Slot "
                 f"{cmd['SourceSlotId']}")

    # InventoryType laut Client-Enum (Namen HeroInventory ... CastleRenovation;
    # belegt: 0 = Helden-Inventar, 6 = Materialsammlung der Burg-Renovierung)
    INVENTORIES: ClassVar[dict[int, str]] = {0: "HeroItems", 6: "CastleRenovationItems"}

    def _inventory(self, kind):
        key = self.INVENTORIES.get(kind)
        if key is None:
            raise ValueError(f"Inventar {kind} nicht unterstützt")
        return self.account["Inventory"].setdefault(key, [])

    def _cmd_InventoryMoveItemCommand(self, cmd):
        """Count Stück von SourceSlotId nach DestinationSlotId; gleiches
        Template stapelt, freier Ziel-Slot nimmt auf, sonst Tausch."""
        src = self._inventory(cmd["SourceInventory"])
        dst = self._inventory(cmd["DestinationInventory"])
        a = next((x for x in src if x["SlotIndex"] == cmd["SourceSlotId"]), None)
        if a is None:
            raise KeyError(f"Slot {cmd['SourceSlotId']} ist leer")
        b = next((x for x in dst if x["SlotIndex"] == cmd["DestinationSlotId"]), None)
        n = min(cmd.get("Count") or a.get("StackCount", 1), a.get("StackCount", 1))
        same = (b is not None and b is not a
                and b["Item"].get("$type") == a["Item"].get("$type")
                and b["Item"].get("TemplateId") == a["Item"].get("TemplateId")
                and a["Item"].get("$type") == T("HeroConsumableItem"))
        if same or (b is None and n < a.get("StackCount", 1)):
            if b is None:
                b = {"$type": T("AccountInventorySlot"), "Item": copy.deepcopy(a["Item"]),
                     "SlotIndex": cmd["DestinationSlotId"], "StackCount": 0}
                dst.append(b)
            b["StackCount"] += n
            a["StackCount"] -= n
            if a["StackCount"] <= 0:
                src.remove(a)
        elif b is None:
            src.remove(a)
            a["SlotIndex"] = cmd["DestinationSlotId"]
            dst.append(a)
        else:  # Tausch
            src.remove(a)
            dst.remove(b)
            a["SlotIndex"], b["SlotIndex"] = b["SlotIndex"], a["SlotIndex"]
            dst.append(a)
            src.append(b)
        self.log(f"    [state] Inventar {cmd['SourceInventory']}/{cmd['SourceSlotId']}"
                 f" -> {cmd['DestinationInventory']}/{cmd['DestinationSlotId']}"
                 f" ({n} Stück)")

    def _cmd_InventorySwapItemCommand(self, cmd):
        """Zwei Slots der Taschen tauschen (finaler Client; Felder nur
        SourceSlotId/DestinationSlotId), leeres Ziel = Verschieben wie
        InventoryManager.MoveItem."""
        slots = self._slots()
        a = next((x for x in slots if x["SlotIndex"] == cmd["SourceSlotId"]), None)
        if a is None or cmd["SourceSlotId"] == cmd["DestinationSlotId"]:
            raise KeyError(f"Slot {cmd['SourceSlotId']} ungültig")
        b = next((x for x in slots if x["SlotIndex"] == cmd["DestinationSlotId"]), None)
        if b is not None:
            b["SlotIndex"] = cmd["SourceSlotId"]
        a["SlotIndex"] = cmd["DestinationSlotId"]
        self.log(f"    [state] Taschen-Slot {cmd['SourceSlotId']} <-> {cmd['DestinationSlotId']}")

    def _cmd_SellHeroItemCommand(self, cmd):
        """Verkauf aus dem Helden-Inventar zum Client-Preis; das Item kommt
        in den Rückkauf (BuyBackId vergibt der Client)."""
        slot = self._take_slot(cmd["SlotIndex"])
        if slot is None:
            raise KeyError(f"Slot {cmd['SlotIndex']} ist leer")
        price = cmd["ClientSellPrice"]
        field = CURRENCY[price["CurrencyType"]]
        got = self._credit(field, price["Amount"])
        self._add_buyback(cmd["BuyBackId"], slot["Item"], slot.get("StackCount", 1),
                          price, "BuyBackTimeout")
        self.log(f"    [state] verkauft: Slot {cmd['SlotIndex']} (Template "
                 f"{cmd.get('TemplateId')}) für {got} {field}")

    # Server-eigene Fälligkeit je Rückkauf-Eintrag {Id: DueDate}; die
    # Haltezeit hängt von der Herkunft ab (ShopSettings.BuyBackTimeout 2h
    # nach Verkauf, InventorySettings.BuyBackTimeoutForInventoryFull 90d für
    # Beute bei vollem Inventar). GetAccountInformation entfernt das Feld.
    BUYBACK_DUE = "BuyBackDue"

    def _add_buyback(self, entry_id, item, count, price, timeout_key):
        """Gegenstand in den Rückkauf; liefert den Eintrag."""
        consumable = item.get("$type") == T("HeroConsumableItem")
        now = datetime.datetime.now(datetime.UTC)
        entry = {"$type": T("ConsumableBuyBackSlot" if consumable
                            else "HeroEquipmentBuyBackSlot"),
                 "Id": entry_id,
                 "CreationDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "ItemType": 4 if consumable else 3,
                 "Price": {"$type": T("CurrencyAmount"), **price},
                 "Item": item}
        if consumable:
            entry["StackCount"] = count
        self.account.setdefault("BuyBack", {"$type": T("AccountBuyBack"),
                                            "BuyBackItems": []})
        self.account["BuyBack"].setdefault("BuyBackItems", []).append(entry)
        settings = (self.data.inventory if timeout_key == "BuyBackTimeoutForInventoryFull"
                    else self.data.shop)
        due = now + datetime.timedelta(seconds=castles.duration(settings[timeout_key]))
        self.account.setdefault(self.BUYBACK_DUE, {})[entry_id] = due.strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return entry

    def _overflow(self, item, count, keep):
        if keep:
            self._overflow_to_buyback(item, count)
        else:
            self.log(f"    [state] Inventar voll: Template {item['TemplateId']} "
                     f"x{count} verloren")

    def _overflow_to_buyback(self, item, count=1):
        """Belohnung ohne freien Inventar-Slot: kostenlos in den Rückkauf
        (wie das Original, BuyBackTimeoutForInventoryFull), dem Client per
        BuyBackAdded gemeldet."""
        # Id wie die des Clients beim Verkauf (ObjectId-Form: Zeitstempel vorn)
        entry_id = f"{int(time.time()):08x}{random.getrandbits(64):016x}"
        entry = self._add_buyback(entry_id, item, count,
                                  {"Amount": 0, "CurrencyType": 2},
                                  "BuyBackTimeoutForInventoryFull")
        self.overflow.append(("BuyBackAdded", {"AccountBuyBackSlot": copy.deepcopy(entry)}))
        self.log(f"    [state] Inventar voll: Template {item['TemplateId']} x{count} "
                 f"in den Rückkauf")

    def _take_overflow(self):
        out, self.overflow = self.overflow, []
        return out

    def _cmd_BuyBackCommand(self, cmd):
        """Rückkauf: Preis zahlen, Item in den/die vom Client genannten Slots."""
        items = self.account.get("BuyBack", {}).get("BuyBackItems", [])
        entry = next((x for x in items if x["Id"] == cmd["BuyBackId"]), None)
        if entry is None:
            raise KeyError(f"Rückkauf {cmd['BuyBackId']} unbekannt")
        price = entry["Price"]
        field = CURRENCY[price["CurrencyType"]]
        wallet = self.account["Wallet"]
        if wallet[field] < price["Amount"]:
            raise ValueError(f"zu wenig {field}")
        targets = {int(i): n for i, n in (cmd.get("SlotIndexes") or {}).items()}
        if not targets:
            # Client nennt manchmal keinen Slot (z. B. nach Verkauf aus einem
            # gerade freigewordenen Slot): niedrigster freier Slot, dem
            # Client per HeroInventoryAdded gemeldet
            index = self._free_slot()
            if index is None:
                raise ValueError("Inventar voll, Rückkauf nicht möglich")
            targets = {index: entry.get("StackCount", 1)}
            self.events.append(("HeroInventoryAdded", {"InventorySlot": {
                "$type": T("AccountInventorySlot"), "Item": entry["Item"],
                "SlotIndex": index, "StackCount": targets[index]},
                "NewlyAdded": True}))
        wallet[field] -= price["Amount"]
        items.remove(entry)
        (self.account.get(self.BUYBACK_DUE) or {}).pop(entry["Id"], None)
        for index, n in targets.items():
            self._put_slot(index, copy.deepcopy(entry["Item"]), n)
        self.log(f"    [state] zurückgekauft: {cmd['BuyBackId']} -> Slots "
                 f"{targets}, -{price['Amount']} {field}")

    def _cmd_HeroEquipmentUnequipCommand(self, cmd):
        hero = self._hero(cmd["HeroId"])
        slot = CLIENT_SLOTS[cmd["SourceSlot"]]
        item = hero["Equipment"].get(slot)
        if not item:
            raise KeyError(f"{slot} ist leer")
        if item.get("TemplateId") == self._default_equipment(cmd["HeroId"]).get(slot):
            raise ValueError(f"{slot}: Standard-Ausrüstung lässt sich nicht ablegen")
        hero["Equipment"][slot] = None
        self._put_slot(cmd["DestinationSlotId"], item)
        self.log(f"    [state] Held {cmd['HeroId']}: {slot} -> Slot "
                 f"{cmd['DestinationSlotId']}")