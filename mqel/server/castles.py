"""PVE-Burgen (Ubisoft-Castles) aus den GameplaySettings des Clients.

Liefert die Angriffsauswahl (AttackSelectionService.GetAttackSelectionList)
in der Form des Update3-Clients; Feldlisten aus dessen JSON-Lesern
(AttackSelectionResult, AttackSelectionByLevelResult, CastleInfoSummary,
AccountSummary). Burg-Id = AccountId der Castle-Datei.
"""

import copy
import datetime
import functools
import glob
import json
import os
import random

import balance
import loot
import patcher
import trophies

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASTLES = os.path.join(ROOT, "client", "GameData", "Data", "GameplaySettings",
                       "Castles")
CONTRACTS = "HyperQuest.GameServer.Contracts"

CASTLE_TYPE_USER, CASTLE_TYPE_UBISOFT = 0, 1

# Ersatzspieler: der Server hat keine anderen Spieler, das Spiel setzt aber
# Spielerburgen voraus (Quest 301 "Friendly Pillage" verlangt CastleTypes
# ["User"], danach zeigt das Tutorial die Spielerburgen). Jede PVE-Burg gibt
# es deshalb zusätzlich als Burg eines erfundenen Spielers (CastleType User,
# AccountId = FAKE_PLAYER_BASE + Id der Vorlage, fester Name).
FAKE_PLAYER_BASE = 1000000
# Länder der Dummy-Spieler (Flaggen in Generated/Web/COMMON/FLAGSSML);
# ohne Land assertet das Clan-Fenster und die Flagge bleibt leer
_COUNTRIES = ["DE", "FR", "US", "GB", "CA", "ES", "IT", "PL", "BR", "SE",
              "NL", "RU", "AT", "CH", "BE", "FI", "NO", "HU", "PT"]
_NAME_A = ["Grim", "Lucky", "Sir", "Mad", "Old", "Brave", "Sneaky", "Golden",
           "Rusty", "Shady", "Mighty", "Lazy", "Iron", "Silver", "Crazy", "Dark"]
_NAME_B = ["Bob", "Greta", "Knuckles", "Barnaby", "Hilda", "Rupert", "Mog",
           "Wilma", "Fenwick", "Tilda", "Gus", "Petunia", "Horace", "Ida", "Bert"]


def T(name):
    return f"{CONTRACTS}.{name}, {CONTRACTS}"


@functools.cache
def building_ranks(spec_id):
    """Rang-Specs eines Gebäudes (Buildings/<spec>/Gameplay.json, "Ranks")."""
    path = glob.glob(os.path.join(loot.SETTINGS, "Buildings", f"{spec_id:06d} - *",
                                  "Gameplay.json"))
    if not path:
        return []

    def ranks(o):
        if isinstance(o, dict):
            if "Ranks" in o:
                return o["Ranks"]
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                r = ranks(v)
                if r:
                    return r
        return []
    with open(path[0], encoding="utf-8") as f:
        return ranks(json.load(f))


def _spec_value(folder, spec_id, key):
    """Erster Wert von `key` in <folder>/<spec>/Gameplay.json (None: fehlt)."""
    path = glob.glob(os.path.join(loot.SETTINGS, folder, f"{spec_id:06d} - *",
                                  "Gameplay.json"))
    if not path:
        return None

    def find(o):
        if isinstance(o, dict):
            if key in o:
                return o[key]
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                r = find(v)
                if r is not None:
                    return r
        return None
    with open(path[0], encoding="utf-8") as f:
        return find(json.load(f))


@functools.cache
def construction_points(folder, spec_id):
    """ConstructionPoints einer Kreatur/Falle/Deko (<folder>/<spec>/Gameplay.json)."""
    return _spec_value(folder, spec_id, "ConstructionPoints") or 0


@functools.cache
def is_rare(folder, spec_id):
    """Seltene Kreatur/Falle (BuildableSpec.IsRare: Limited Edition, Halloween)."""
    return bool(_spec_value(folder, spec_id, "IsRare"))


def rare_ratio(c):
    """Loot-O-Meter (PotentialLoot.RareDefenseIngredientRatio): Beute-Bonus
    für seltene Verteidigung, der Client zeigt ihn als "+N%". Nur
    Spielerburgen (bei Ubisoft-Burgen zeigt der Client statt des Werts einen
    festen Untertitel). Die Formel lag nur auf dem Originalserver (die DLL
    von 2014 kennt das Feld nicht); hier: Anteil seltener Kreaturen und
    Fallen an deren Bau-Punkten mal RareDefenseIngredientBonusFactor,
    mindestens RareDefenseIngredientBonusMinimumRatio, höchstens 1. Wirkt
    als Faktor auf Gold/Lebenskraft (loot.creature_loot) und wie Magic Find
    auf die Qualität der Kreatur-Drops (attack.py)."""
    if castle_type(c) != CASTLE_TYPE_USER:
        return 0.0
    total = rare = 0
    for r in c.get("Rooms") or []:
        for folder in ("Creatures", "Traps"):
            for x in r.get(folder) or []:
                cp = construction_points(folder, x["SpecContainerId"])
                total += cp
                if is_rare(folder, x["SpecContainerId"]):
                    rare += cp
    if not rare:
        return 0.0
    s = loot.rewards()
    ratio = max(s["RareDefenseIngredientBonusFactor"] * rare / total,
                s["RareDefenseIngredientBonusMinimumRatio"])
    return round(min(ratio, 1.0), 2)


def boost_spec(boost_id):
    """DefenseIngredientBoostSpec (BoostTarget, Duration)."""
    path = glob.glob(os.path.join(loot.SETTINGS, "DefenseIngredientBoost",
                                  f"{boost_id:06d} - *", "Gameplay.json"))[0]
    with open(path, encoding="utf-8") as f:
        return json.load(f)[0]


def duration(text):
    """Zeitangabe der Spieldaten ("5m", "1h30m", "45s", "2d") in Sekunden."""
    total, num = 0, ""
    for ch in text:
        if ch.isdigit():
            num += ch
        else:
            total += int(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[ch]
            num = ""
    return total + int(num or 0)


@functools.cache
def building_instances(spec_id):
    """Je erlaubter Instanz eines Gebäudes die nötige Burgherz-Stufe
    (BuildingInstanceRequirements, z. B. Bling-Mine [2, 2, 2]); [] ohne Datei."""
    path = glob.glob(os.path.join(loot.SETTINGS, "Buildings", f"{spec_id:06d} - *",
                                  "Gameplay.json"))
    if not path:
        return []
    with open(path[0], encoding="utf-8") as f:
        spec = json.load(f)
    spec = spec[0] if isinstance(spec, list) else spec
    return [r["BuildingRequirementRank"] for r in spec.get("BuildingInstanceRequirements") or []
            if r.get("BuildingRequirementSpecContainerId") == 1]


def mine_rank(spec_id, rank):
    """MineBuildingRankSpec (ProductionPeriod/-Value, Capacity) oder None."""
    r = building_ranks(spec_id)
    if 0 < rank <= len(r) and r[rank - 1]["$type"].startswith(
            "HyperQuest.GameServer.Contracts.MineBuildingRankSpec"):
        return balance.mine_rank(spec_id, r[rank - 1])  # Bling-Mine: Balancing
    return None


def _clamp_building_ranks(c):
    """Einige Ubisoft-Burgen (14-20, 26, 52-54 ...) haben Gebäude über dem
    höchsten Rang (Burgherz 34 statt 30, Lager bis 13 statt 5); der Client
    startet den Angriff dann nicht (Burg 14, Quest 306 "Who's the Boss?")."""
    for r in c.get("Rooms") or []:
        for b in r.get("Buildings") or []:
            top = len(building_ranks(b["SpecContainerId"]))
            if top and b.get("Rank", 1) > top:
                b["Rank"] = top


# Saison-Burgen (Datei mit IsDisabled): nur während ihres Events
# (patcher.seasonal_event) angreifbar, in der Region ihres Levels
EVENT_CASTLES = {"Halloween": range(82, 90)}


def _disabled(c):
    if not c.get("IsDisabled"):
        return False
    event = patcher.seasonal_event(datetime.date.today())
    return c.get("AccountId") not in EVENT_CASTLES.get(event, ())


def active_event(c):
    """Name des laufenden Events, wenn c eine seiner Saison-Burgen ist."""
    event = patcher.seasonal_event(datetime.date.today())
    return event if c.get("AccountId") in EVENT_CASTLES.get(event, ()) else None


def load_pve_castles():
    """Aktive PVE-Burgen mit Level. Tutorial-Burgen (2, 3) haben keins,
    gehören aber in die Auswahl (Assignment 150 zeigt auf Burg 3, später
    blendet der Client sie per HideAttackSelectionCastles aus): Level 1.
    Kauf-Burgen bleiben draußen."""
    out = []
    for path in sorted(glob.glob(os.path.join(CASTLES, "*.json"))):
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("IsTutorialCastle"):
            c.setdefault("Level", 1)
        _oasis_name(c)
        if _disabled(c) or c.get("Level") is None:
            continue
        _clamp_building_ranks(c)
        out.append(c)
    return out


CP_FOLDERS = {"Creatures": "Creatures", "Traps": "Traps", "Decorations": "Decorations"}


def total_construction_points(c):
    """Belegte Bau-Punkte der Burg (Kreaturen, Fallen, Deko)."""
    return sum(construction_points(folder, x["SpecContainerId"])
               for r in c.get("Rooms") or [] for key, folder in CP_FOLDERS.items()
               for x in r.get(key) or [])


# Par-Zeit (Sekunden) für Burgen ohne Validierung, aus den Bau-Punkten
# geschätzt: die Tabelle (TreasureChestLockCountdown: CPFrom/CPTo,
# DurationsByVictoryCondition BaseDuration/CPMultiplier) lag nur auf dem
# Originalserver. Ohne Par-Zeit zeigt der Client keinen Timer und streicht
# den zweiten Stern sofort.
PAR_BASE, PAR_PER_CP, PAR_MAX = 30, 3, 300


def par_time(c):
    """CastleValidationDuration: Zeit der Validierung, sonst geschätzt."""
    return float(c.get("CastleValidationDuration")
                 or min(PAR_MAX, PAR_BASE + PAR_PER_CP * total_construction_points(c)))


def max_construction_points(c, rank=None):
    """Bau-Punkte-Grenze nach Rang des Burgherzens (CastleHeart Ranks)."""
    rank = rank or heart_rank(c)
    if not rank:
        return 0
    path = glob.glob(os.path.join(loot.SETTINGS, "Buildings", "000001 - *", "Gameplay.json"))
    with open(path[0], encoding="utf-8") as f:
        ranks = json.load(f)[0]["Ranks"]
    return ranks[min(rank, len(ranks)) - 1].get("MaxConstructionPoints", 0)


DIFFICULTY = {"TooEasy": 0, "Easy": 1, "Medium": 2, "Hard": 3, "TooHard": 4}


def difficulty(castle_level, hero_level):
    """CastleDifficulty aus CastleSettings.CastleDifficultyTable
    (Burg-Level minus Helden-Level; fehlende Felder = 0 bzw. TooEasy)."""
    path = os.path.join(loot.SETTINGS, "GeneralSettings", "CastleSettings.json")
    with open(path, encoding="utf-8") as f:
        table = json.load(f)["CastleDifficultyTable"]
    diff = castle_level - hero_level
    for row in table:
        if row.get("LevelFrom", 0) <= diff <= row.get("LevelTo", 0):
            return DIFFICULTY[row.get("CastleDifficulty", "TooEasy")]
    return DIFFICULTY["TooHard"] if diff > 0 else 0


def heart_rank(c):
    """Rang des Burgherzens (Building 1), 0 ohne Herz."""
    return max([b.get("Rank", 1) for r in c.get("Rooms") or []
                for b in r.get("Buildings") or [] if b["SpecContainerId"] == 1]
               or [0])


def castle_type(c):
    return c.get("_CastleType", CASTLE_TYPE_UBISOFT)


ROOMS = os.path.join(os.path.dirname(CASTLES), "Rooms")
_ORIENTATION = {"Front": 0, "Left": 1, "Back": 2, "Right": 3}
ENTRANCES = (21, 22)
THRONES = (25, 26, 27, 7046)
GALLERY_ROOMS = 4  # Gänge zwischen Eingang und Thronsaal


@functools.cache
def _room_sections():
    out = {}
    for path in glob.glob(os.path.join(ROOMS, "*", "Gameplay.json")):
        sid = int(os.path.basename(os.path.dirname(path)).split(" ")[0])
        with open(path, encoding="utf-8") as f:
            for comp in json.load(f):
                if comp["$type"].split(",")[0].endswith(".RoomSpec"):
                    out[sid] = comp.get("RoomSections", [])
    return out


def _is_linear(spec):
    """Gang mit Durchgang nach -X (Passages[1]) und +X (Passages[3]) bei
    Ausrichtung 0 (Richtungen belegt durch die Startburg: Eingang 21 links
    an Thronsaal 25 angeschlossen)."""
    secs = _room_sections().get(spec, [])
    if len(secs) != 1:
        return False
    p = secs[0]["Passages"]
    return p[1] == p[3] == "Passage" and "Passage" not in (p[0], p[2])


def _orientation(r):
    o = r.get("Orientation") or 0
    if isinstance(o, str):  # Name ("Front"...) statt Zahl
        o = _ORIENTATION[o]
    return o % 4


@functools.cache
def _room_pool():
    """Räume der PVE-Burgen mit Ausrichtung 0 samt Inhalt, je Art:
    [(Burg-Level, Burg-Id, Raum)]. Nur ungedrehte Räume: in gedrehten
    lassen sich die Inhalte in Spielerburgen nicht setzen."""
    pool = {"entrance": [], "throne": [], "gallery": []}
    for c in load_pve_castles():
        if c.get("IsTutorialCastle"):
            continue
        for r in c.get("Rooms") or []:
            if _orientation(r):
                continue
            spec = r["SpecContainerId"]
            kind = ("entrance" if spec in ENTRANCES else "throne" if spec in THRONES
                    else "gallery" if _is_linear(spec) else None)
            if kind:
                pool[kind].append((c["Level"], c["AccountId"], r))
    return pool


@functools.cache
def _required_rank(kind, spec):
    """BuildingRequirementSpec-Rang (Architektenbüro o. ä.) einer Spec, 0
    ohne Voraussetzung. Spielerburgen prüfen ihn beim Laden ("Cannot snap
    loaded buildable entity": Gang 2006 verlangt Rang 5 und fiel in einer
    Level-3-Burg heraus, Gänge mit Rang 2 luden)."""
    path = glob.glob(os.path.join(os.path.dirname(CASTLES), kind,
                                  f"{spec:06d} - *", "Gameplay.json"))
    if not path:
        return 0
    with open(path[0], encoding="utf-8") as f:
        return max([comp.get("BuildingRequirementRank", 0) for comp in json.load(f)
                    if comp["$type"].split(",")[0].endswith(".BuildingRequirementSpec")]
                   or [0])


def _nearest(entries, level, castle_id, n):
    """n Einträge, bevorzugt aus der Vorlage, dann nach Level-Abstand."""
    ranked = sorted(entries, key=lambda e: (e[1] != castle_id, abs(e[0] - level), e[1]))
    return ranked[:n]


ARCHITECT_THRONE_ROOM = 25
FAKE_CREATURES_PER_ROOM = 1

_REFS = ("PowerSupplyCastleBuildableId", "TotemCastleBuildableId")


@functools.cache
def _player_buildable(kind, spec):
    """Spec im Baumodus verfügbar (IsAvailableInBuildMode)? PVE-Burgen
    enthalten Level-Design-Objekte (z. B. Trap 77 LD_FloorLoot_Gold), die
    sich in Spielerburgen nicht setzen lassen ("Cannot snap loaded
    buildable entity to the room grid" -> Raum fehlt -> Burg ungültig)."""
    path = glob.glob(os.path.join(os.path.dirname(CASTLES), kind,
                                  f"{spec:06d} - *", "Gameplay.json"))
    if not path:
        return False
    with open(path[0], encoding="utf-8") as f:
        comps = json.load(f)
    # Kanonen hängen an Wänden; in der Vorlage stand die Wand an einem
    # Durchgang ohne Nachbarraum, in der generierten Reihe ist dort ein Raum
    if any(comp.get("RoomBuildableType") == "Cannon" for comp in comps):
        return False
    return any(comp.get("IsAvailableInBuildMode") for comp in comps)


def _generated_rooms(c):
    """Spielerburg nach den Regeln des Clients (AttackManager, "Loaded
    invalid castle"): ein Eingang, ein Thronsaal, durchgehender Weg, keine
    losen Räume. Aufbau: Thronsaal links, GALLERY_ROOMS Gänge, Eingang
    rechts, alles in einer Reihe mit Ausrichtung 0; Räume samt Kreaturen,
    Fallen und Deko aus PVE-Burgen ähnlichen Levels."""
    pool = _room_pool()
    level, cid = c["Level"], c["AccountId"]
    allowed = {k: [e for e in v
                   if _required_rank("Rooms", e[2]["SpecContainerId"]) <= level]
               for k, v in pool.items()}
    # nur Thronsaal 25: einzige Vorlage mit Platz fürs Architektenbüro
    throne = _nearest([e for e in allowed["throne"]
                       if e[2]["SpecContainerId"] == ARCHITECT_THRONE_ROOM],
                      level, cid, 1)
    entrance = _nearest(allowed["entrance"], level, cid, 1)
    galleries = _nearest(allowed["gallery"], level, cid, GALLERY_ROOMS)
    if not (throne and entrance and len(galleries) == GALLERY_ROOMS):
        return None
    row = throne + galleries + entrance
    x0 = 1 + max(-s.get("X", 0) for s in _room_sections()[throne[0][2]["SpecContainerId"]])
    rooms = []
    for i, (_, src, room) in enumerate(row):
        r = json.loads(json.dumps(room))
        r.update(Id=i + 1, X=x0 + i, Y=4, Orientation=0)
        # Deko ganz weg (rein optisch, setzte sich mehrfach nicht), sonst
        # nur Spieler-Baubares
        r["Decorations"] = []
        r["Traps"] = []  # setzten sich mehrfach nicht (Minenfelder, Kanonen)
        r["Creatures"] = [e for e in r.get("Creatures") or []
                          if _player_buildable("Creatures", e["SpecContainerId"])
                          and _required_rank("Creatures", e["SpecContainerId"]) <= level]
        # leicht halten: je Raum nur die schwächste Kreatur
        r["Creatures"] = sorted(r["Creatures"],
                                key=lambda e: loot.creature(e["SpecContainerId"])[0]
                                )[:FAKE_CREATURES_PER_ROOM]
        # Entity-Ids je Quellburg verschieben, damit Räume verschiedener
        # Vorlagen nicht kollidieren; Querverweise mitziehen
        offset = (i + 1) * 1000
        for key in ("Buildings", "Creatures", "Traps", "Decorations", "Triggers"):
            for e in r.get(key) or []:
                e["Id"] += offset
                for ref in _REFS:
                    if e.get(ref):
                        e[ref] += offset
        rooms.append(r)
    ids = {e["Id"] for r in rooms for k in ("Buildings", "Creatures", "Traps",
                                            "Decorations", "Triggers")
           for e in r.get(k) or []}
    for r in rooms:  # Verweise auf Räume, die nicht übernommen wurden
        for k in ("Creatures", "Traps", "Decorations"):
            for e in r.get(k) or []:
                for ref in _REFS:
                    if e.get(ref) and e[ref] not in ids:
                        e[ref] = 0
    return rooms


def _fake_player(c):
    """Burg eines erfundenen Spielers zur PVE-Burg `c` (Level, Thema,
    Räume aus PVE-Burgen ähnlichen Levels), oder None."""
    rooms = _generated_rooms(c)
    if rooms is None:
        return None
    src = c["AccountId"]
    f = {k: v for k, v in c.items()
         # ohne die Oasis-Namen der Vorlage: mit OasisNameId zeigt der
         # Client den Burgnamen ("Alien Outpost") statt des Spielers
         if k not in ("OasisName", "OasisNameId", "OasisDescription",
                      "CustomAttackerReward", "IsTutorialCastle")}
    f["AccountId"] = FAKE_PLAYER_BASE + src
    f["AccountDisplayName"] = (f"{_NAME_A[src % len(_NAME_A)]}"
                               f"{_NAME_B[src * 7 % len(_NAME_B)]}")
    f["_CastleType"] = CASTLE_TYPE_USER
    f["CountryCode"] = _COUNTRIES[src * 5 % len(_COUNTRIES)]
    f["LayoutId"] = 1  # einziges Spielerburg-Raster (CastleLayouts, 10x10)
    f["Rooms"] = rooms
    # Raumzahl einer Spielerburg begrenzt das Architektenbüro (Building 13,
    # Ranks MaxRooms 3..8; CastleCellGrid::CanSnapEntityToGrid, Fehlerbit
    # 0x200 CannotPlaceMoreRooms, nur bei Spielerburgen); Platz wie in der
    # Startburg (Thronsaal 25, Zone 7)
    rooms[0].setdefault("Buildings", []).append({
        "$type": T("CastleBuilding"), "Id": 999, "SpecContainerId": 13,
        "Rank": 6, "RoomZoneId": 7, "X": 3, "Orientation": 3})
    # Burgherz-Rang bestimmt die Baupunkte-Obergrenze (CastleHeart Ranks
    # MaxConstructionPoints 20..375), die der Client bei Spielerburgen
    # prüft (bei PVE nicht): höchster Rang, damit alle Kreaturen passen
    for r in rooms:
        for b in r.get("Buildings") or []:
            if b["SpecContainerId"] == 1:
                b["Rank"] = 30
    return f


import threading as _threading
import time as _time

# fake_player_castles() reads ~8500 castle JSON files. player_castle() calls it
# for every real player without a published castle, so one attack selection
# (More Castles / GetAttackSelectionList) rebuilt it once per player: ~4-5 s
# with 174 accounts. Cache it; the TTL keeps time-limited event castles fresh.
_FAKES_TTL = 300  # seconds
_fakes_cache = (0.0, None)
_fakes_lock = _threading.Lock()


def fake_player_castles():
    """Ersatzspieler (zwischengespeichert, siehe _build_fake_player_castles).
    Liefert eine neue Liste, die Burg-Dicts sind geteilt: nicht veraendern,
    vorher kopieren (player_castle macht das schon)."""
    global _fakes_cache
    stamp, value = _fakes_cache
    if value is not None and _time.monotonic() - stamp < _FAKES_TTL:
        return list(value)
    with _fakes_lock:
        stamp, value = _fakes_cache
        if value is None or _time.monotonic() - stamp >= _FAKES_TTL:
            value = _build_fake_player_castles()
            _fakes_cache = (_time.monotonic(), value)
        return list(value)


def _build_fake_player_castles():
    """Ersatzspieler aus den Ubisoft-Burgen ohne Tutorial- und Bossburgen."""
    bosses = {r.get("BossCastleId") for r in attack_regions().values()}
    return [f for f in (_fake_player(c) for c in load_pve_castles()
                        if not c.get("IsTutorialCastle")
                        and c["AccountId"] not in bosses) if f]


# Echte Spieler (players.Players), setzt game_server beim Start
_players = None


def register_players(players):
    global _players
    _players = players


def published_rooms(account):
    """Räume der zuletzt validierten Burg eines echten Spielers oder None."""
    return (account.get("PublishedRooms")
            or (account.get("ValidBlueprint") or {}).get("Rooms"))


def shield_due(account, now=None):
    """DueDate des aktiven Burgschilds (ShieldEndExpirable, verwiesen von
    BuildInfo.ShieldEndExpirableId) oder None."""
    eid = (account.get("BuildInfo") or {}).get("ShieldEndExpirableId")
    stamp = (now or datetime.datetime.now(datetime.UTC)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return next((e["DueDate"] for e in account.get("Expirables") or []
                 if e["Id"] == eid and e.get("ExpirableType") == "ShieldEnd"
                 and e["DueDate"] > stamp), None)


def _mine_statuses(account, rooms):
    """MineStatuses für die Beute (loot.mine_stealable), je Mine ergänzt um
    _Shielded (Minenschild läuft) und _HourlyProduction (Ertrag in
    AttackSettings.MinePeriodStealableAmountInSeconds, Vorgabe des Originals
    3600 s, nach dem Rang im übergebenen Burgstand)."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    active = {e["Id"] for e in account.get("Expirables") or []
              if e.get("ExpirableType") == "MineShield" and e["DueDate"] > stamp}
    period = loot.attack_settings().get("MinePeriodStealableAmountInSeconds", 3600)
    buildings = {str(b["Id"]): b for r in rooms for b in r.get("Buildings") or []}
    out = copy.deepcopy(account["BuildInfo"].get("MineStatuses") or {})
    for bid, s in out.items():
        b = buildings.get(bid)
        spec = b and mine_rank(b["SpecContainerId"], b.get("Rank", 0))
        s["_Shielded"] = s.get("ShieldExpirableId") in active
        s["_HourlyProduction"] = (period * spec["ProductionValue"] / duration(spec["ProductionPeriod"])
                                  if spec else 0)
    return out


def _own_throne_room(c, own_rooms):
    """Gebäude im Thronsaal (25) der Ersatzburg c durch die des Spielers
    ersetzen: Minen, Lager und Plünderung sind seine. Burgherz und
    Architektenbüro behalten mindestens den Rang der Ersatzburg (Baupunkte
    und Raumzahl ihrer Kreaturen und Räume, _fake_player)."""
    own = next((r for r in own_rooms if r["SpecContainerId"] == 25), None)
    throne = next((r for r in c["Rooms"] if r["SpecContainerId"] == 25), None)
    if not own or not throne:
        return
    ranks = {b["SpecContainerId"]: b.get("Rank", 0) for b in throne.get("Buildings") or []
             if b["SpecContainerId"] in (1, 13)}
    throne["Buildings"] = copy.deepcopy(own.get("Buildings") or [])
    for b in throne["Buildings"]:
        if b["SpecContainerId"] in ranks:
            b["Rank"] = max(b.get("Rank", 0), ranks[b["SpecContainerId"]])


def player_castle(account, draft=False, rooms=None):
    """Burg eines echten Spielers in Form einer Burgdatei: mit draft der
    Entwurf (Validierung), mit rooms diese Räume (delegierte Validierung),
    sonst der zuletzt validierte Stand
    (AccountState.PUBLISHED_ROOMS; ältere Spielstände: VALID_BLUEPRINT).
    Noch nie validiert: eine Ersatzspieler-Burg ähnlicher Stufe (sonst wäre
    das Konto durch Nicht-Validieren unangreifbar; ein unvalidierter
    Entwurf ist evtl. leer, den lehnt der Client ab), Konto, Kronen,
    Schild, Schatzkammer und Thronsaal-Gebäude mit den Minen
    (_own_throne_room) bleiben die des Spielers. None: keine Räume.
    IsDraftPublished taugt nicht: die Kontovorlage setzt es schon für die
    leere Startburg, und der Server nimmt es bei Änderungen nicht zurück."""
    build = account["BuildInfo"]
    level = build.get("Level", 1)
    fallback = rooms is None and not draft
    if rooms is None:
        rooms = build["Draft"]["Rooms"] if draft else published_rooms(account)
    if not rooms and not fallback:
        return None
    if rooms:
        c = copy.deepcopy(build["Draft"])
        c.update(Rooms=copy.deepcopy(rooms), IsTutorialCastle=False,
                 ForceCastleLevelOnBuildables=False,
                 CastleValidationDuration=float(account.get("ValidationDuration", 0)))
    else:
        fakes = sorted(fake_player_castles(), key=lambda f: f["AccountId"])
        if not fakes:
            return None
        best = min(abs(f["Level"] - level) for f in fakes)
        near = [f for f in fakes if abs(f["Level"] - level) == best]
        c = copy.deepcopy(near[account["AccountId"] % len(near)])
        _own_throne_room(c, build["Draft"]["Rooms"])
    c.update(Level=level, AccountId=account["AccountId"],
             AccountDisplayName=account.get("DisplayName") or "",
             CountryCode=account.get("CountryCode") or "",
             TrophyScore=account.get("TrophyScore", 0),
             PublishedCastleId="", OasisNameId=0, _CastleType=CASTLE_TYPE_USER,
             # Füllstand, Minenschild und Produktion: Grundlage der Minenbeute
             _MineStatuses=_mine_statuses(account, c["Rooms"]),
             _ShieldDue=shield_due(account),
             # Schatzkammer: ein Anteil davon ist plünderbar (loot.treasure_room)
             _Wallet={f: account["Wallet"].get(f, 0) for f in ("InGameCoin", "LifeForce")})
    return c


# Delegierte Validierung (AccountState.VALIDATION_DELEGATION): die Burg im
# Stand beim Kauf des Tickets erscheint anderen Spielern (beim Clan-Ticket
# nur Clanmitgliedern) in der Angriffsregion des Tickets mit AttackType
# Validation; der erste Sieg validiert sie.
ATTACK_TYPE_VALIDATION = 1


def validation_castle(account, viewer_id, may_validate):
    """Burg im Stand des Validierungs-Tickets, wenn viewer_id sie validieren
    darf (nicht der Besitzer; may_validate(Besitzer-Id, Clan-Ticket)),
    sonst None. _ValidationRegion = Angriffsregion des Tickets."""
    d = account.get("ValidationDelegation")
    if (not d or account["AccountId"] == viewer_id
            or not may_validate(account["AccountId"], d["Guild"])):
        return None
    c = player_castle(account, rooms=d["Rooms"])
    if c is None:  # Ticket ohne Räume
        return None
    c.update(_MineStatuses=copy.deepcopy(d["MineStatuses"]),
             _ValidationRegion=d["Region"], _ValidationGuild=d["Guild"])
    return c


def validation_reward(c):
    """CompletionReward einer Burg mit Validierungs-Ticket: das bezahlte
    Ticket bringt DelegatedCastleValidationReward, das Clan-Ticket nichts
    (Vorlage template-validation-attackReport-content: ohne Belohnung der
    Dank fürs Validieren der Burg eines Clanmitglieds)."""
    if not c or "_ValidationRegion" not in c or c["_ValidationGuild"]:
        return []
    return [{"$type": T("CurrencyAmount"), "Amount": r["Amount"],
             "CurrencyType": r["CurrencyType"]}
            for r in loot.rewards()["DelegatedCastleValidationReward"]]


def validation_castles(viewer_id, may_validate):
    """Burgen echter Spieler, die viewer_id validieren darf."""
    out = []
    for p in (_players.all() if _players else []):
        c = validation_castle(p.state.snapshot(), viewer_id, may_validate)
        if c:
            out.append(c)
    return out


def _fresh_snapshot(p):
    """Spielstand (Kopie) mit abgerechneten Material-O-Matic-Perioden: sonst
    sähe ein Angreifer das Paket vom letzten Besuch des Besitzers (nach
    einer Woche Abwesenheit leer statt Stufe 6). Die Meldungen gehen in
    die Warteschlange des Besitzers."""
    for name, fields in p.state.material_mines(datetime.datetime.now(datetime.UTC)):
        p.notifications.push(name, fields)
    return p.state.snapshot()


def player_account(account_id):
    """Spielstand eines echten Spielers (Kopie) oder None."""
    p = _players and _players.by_account_id(account_id)
    return _fresh_snapshot(p) if p else None


def real_player_castles(exclude=None):
    """Angreifbare Burgen echter Spieler (Tutorial durch: Anzeigename,
    veröffentlichte Burg), ohne den Account exclude."""
    out = []
    for p in (_players.all() if _players else []):
        a = _fresh_snapshot(p)
        if (a["AccountId"] != exclude and a.get("DisplayName")
                and a.get("IsCastleAttackable", True)):
            c = player_castle(a)
            if c:
                out.append(c)
    return out


def castle_summary(c):
    return {
        "$type": T("CastleInfoSummary"),
        "AttackerStatus": 0,  # Normal
        "CastleHeartRank": 0,
        "CastleThemeId": c.get("ThemeId", 1),
        "CastleType": castle_type(c),
        "DefenderAccountSummary": {
            "$type": T("AccountSummary"),
            "AvatarId": 0,
            "CastleLevel": c["Level"],
            "CountryCode": c.get("CountryCode", ""),
            "DisplayName": c.get("AccountDisplayName", ""),
            "Id": c["AccountId"],
            "IsCastleAttackable": True,
            "LeagueId": 0,
            "OasisNameId": c.get("OasisName", 0),
            "SpecialPacks": [],
            "SubLeagueId": 0,
            "TrophyScore": 0,
        },
        "FromMachineLearning": False,
        "IsBranded": False,
        "IsCastleAttackable": True,
        "IsNew": False,
        "IsShielded": False,
        "Level": c["Level"],
        "TrophyScore": 0,
        "VictoryConditionLevel": 0,
        "WinRatioDifficulty": 0,
    } if castle_type(c) == CASTLE_TYPE_UBISOFT else _user_summary(c)


def _user_summary(c):
    """Spielerburg wie im Original-Mitschnitt (MQELOffline,
    AttackSelectionService.cpp): veröffentlicht (LastPublishedDate),
    Herz-Rang = Level, WinRatioDifficulty 2, IsNew."""
    return {
        "$type": T("CastleInfoSummary"),
        "AttackerStatus": 0,
        "CastleHeartRank": heart_rank(c),
        "CastleThemeId": c.get("ThemeId", 1),
        "CastleType": CASTLE_TYPE_USER,
        "DefenderAccountSummary": {
            "$type": T("AccountSummary"),
            "AvatarId": 10,
            "CastleLevel": c["Level"],
            "DisplayName": c["AccountDisplayName"],
            "Id": c["AccountId"],
            "LeagueId": trophies.league_of(trophies.castle_score(c))[0],
            "SubLeagueId": trophies.league_of(trophies.castle_score(c))[1],
            "TrophyScore": trophies.castle_score(c),
        },
        "TrophyScore": trophies.castle_score(c),
        "IsCastleAttackable": True,
        "IsNew": True,
        "IsShielded": bool(c.get("_ShieldDue")),
        "LastPublishedDate": "2016-10-01T12:00:00Z",
        "Level": c["Level"],
        "WinRatioDifficulty": 2,
    }


ASSIGNMENTS = os.path.join(os.path.dirname(CASTLES), "Assignments")


@functools.cache
def _hidden_by_assignment():
    """{AssignmentId: [Burg-Ids]} aus HideAttackSelectionCastles-Aktionen
    (z. B. 150 blendet die Tutorial-Burgen 2, 3 und 71 aus)."""
    out = {}
    for path in glob.glob(os.path.join(ASSIGNMENTS, "*", "Gameplay.json")):
        aid = int(os.path.basename(os.path.dirname(path)).split(" ")[0])
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        for a in (spec[0] if isinstance(spec, list) else spec).get("Actions", []):
            if a["$type"].split(",")[0].endswith(".HideAttackSelectionCastlesAssignmentActionSpec"):
                out.setdefault(aid, []).extend(a.get("CastleIds", []))
    return out


@functools.cache
def attack_regions():
    """{AttackRegionId: Region} aus AttackAdvisorSettings."""
    with open(os.path.join(loot.SETTINGS, "GeneralSettings",
                           "AttackAdvisorSettings.json"), encoding="utf-8") as f:
        return {r["AttackRegionId"]: r for r in json.load(f)["AttackRegions"]}


def attack_selection(max_per_level=10, completed_assignments=(), region_id=None,
                     reroll=0, friend_ids=(), own_id=None, competitions=None,
                     validations=()):
    """Burgauswahl einer Angriffsregion (AttackAdvisorSettings):
    Friends zeigt die Burgen der Freunde (friend_ids), Competitions die
    Burgen der laufenden Wettbewerbe (competitions: {Burg-Id: Wettbewerbs-Id},
    als UbisoftCompetitionId an der Burg, sonst öffnet der Client nicht das
    Wettbewerbs-Popup), die übrigen
    Regionen Ubisoft- bzw. Spielerburgen (ShowUbisoftCastle/ShowUserCastle)
    im Level-Bereich LevelMin..LevelMax. Burgen, die abgeschlossene
    Assignments ausblenden, fehlen (der Client blendet sie nur bis zum
    Neustart aus). reroll > 0 ("Reroll castles in the region") mischt die
    Burgen je Region und Zähler reproduzierbar. validations: Burgen mit
    Validierungs-Ticket (validation_castles); die der Region kommen zuerst,
    mit AttackType Validation, ohne Level-Grenze und Deckelung. Die
    Bossburg der Region (BossCastleId) steht wie im Original
    (AttackSelectionManager) getrennt in BossCastle; Bossburgen aller
    Regionen fehlen in der normalen Liste. Tutorial-Burgen bleiben drin, bis
    Assignment 150 sie ausblendet: der Client holt die Liste nach dem ersten
    Tutorial-Angriff und richtet 150 auf Burg 3 darin aus."""
    region = attack_regions().get(region_id) or {}
    bosses = {r.get("BossCastleId") for r in attack_regions().values()} - {None, 0}
    hidden = {cid for aid in completed_assignments
              for cid in _hidden_by_assignment().get(aid, [])}
    if region.get("IsFriendRegion"):
        groups = [[c for c in (find_castle(i) for i in friend_ids) if c]]
    elif region.get("IsCompetitionRegion"):
        groups = [[c for c in (find_castle(i) for i in competitions or {}) if c]]
    else:
        lo, hi = region.get("LevelMin", 0), region.get("LevelMax", 10 ** 6)
        groups = []
        if not region or region.get("ShowUbisoftCastle"):
            groups.append([[c for c in load_pve_castles() if c["AccountId"] not in hidden
                            and c["AccountId"] not in bosses]])
        if not region or region.get("ShowUserCastle"):
            # echte Spieler getrennt und vorn, damit weder das Mischen
            # noch die Deckelung sie hinter Ersatzspieler verdrängt
            groups.append([real_player_castles(own_id), fake_player_castles()])
        groups = [[[c for c in part if lo <= c["Level"] <= hi] for part in g]
                  for g in groups]
        rng = random.Random(f"{region_id}/{reroll}")
        groups = [[c for part in g for c in (rng.sample(part, len(part))
                                             if reroll else part)]
                  for g in groups]
    by_level = {}
    validating = {c["AccountId"]: c for c in validations
                  if c["_ValidationRegion"] == region_id}
    for c in validating.values():
        by_level.setdefault(c["Level"], []).append(c)
    groups = [[c for c in g if c["AccountId"] not in validating] for g in groups]
    # je Level getrennt gedeckelt, damit beide Burgarten erscheinen
    for group in groups:
        level_counts = {}
        for c in group:
            n = level_counts.get(c["Level"], 0)
            if n < max_per_level:
                by_level.setdefault(c["Level"], []).append(c)
                level_counts[c["Level"]] = n + 1
    boss = region.get("BossCastleId") and find_castle(region["BossCastleId"])
    return {
        "$type": T("AttackSelectionResult"),
        "BossCastle": castle_summary(boss) if boss else None,
        "CastlesByLevel": [
            {"$type": T("AttackSelectionByLevelResult"),
             "Castles": [dict(castle_summary(c), UbisoftCompetitionId=(
                 competitions or {}).get(c["AccountId"], 0)
                 if region.get("IsCompetitionRegion") else 0,
                 AttackType=ATTACK_TYPE_VALIDATION if c["AccountId"] in validating
                 else 0) for c in cs],
             "Level": level}
            for level, cs in sorted(by_level.items())
        ],
    }


def find_castle(castle_id):
    """Burgdatei zur Id, auch Tutorial-Burgen (2, 3) ohne Level: die
    bekommen Level 1 (Neu-Account mit Level-1-Held). Ids ab
    FAKE_PLAYER_BASE sind Ersatzspieler-Burgen, Accounts echter Spieler
    deren veröffentlichte Burg."""
    account = player_account(castle_id)
    if account is not None:
        return player_castle(account)
    if castle_id >= FAKE_PLAYER_BASE:
        # nur Vorlagen-Burgen (Id < FAKE_PLAYER_BASE) haben Ersatzspieler;
        # sonst rekursiert eine riesige Id bis zum Stapelüberlauf
        template = castle_id - FAKE_PLAYER_BASE
        c = find_castle(template) if template < FAKE_PLAYER_BASE else None
        return (_fake_player(c) if c and not c.get("IsTutorialCastle")
                else None)
    path = _castle_paths().get(castle_id)
    if path is None:
        return None
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    if _disabled(c):
        return None
    c.setdefault("Level", 1)
    _oasis_name(c)
    _clamp_building_ranks(c)
    return c


def _oasis_name(c):
    """Ubisoft-Burgen: Namens-Id der Vorlage (OasisName, z. B. 2645 =
    "Snotter King's Castle") auch als OasisNameId; nur damit zeigt der
    Client den übersetzten Namen statt AccountDisplayName ("Snotter_King")."""
    if not c.get("OasisNameId") and c.get("OasisName"):
        c["OasisNameId"] = c["OasisName"]


@functools.cache
def _castle_paths():
    """{AccountId: Pfad}; Dateinummer und AccountId stimmen nicht immer
    überein (z. B. 000004 - PVE_01_Shamblington)."""
    out = {}
    for path in glob.glob(os.path.join(CASTLES, "*.json")):
        with open(path, encoding="utf-8") as f:
            out[json.load(f).get("AccountId")] = path
    return out


def castle_info(castle_id, attack_type=0, attack_source=0, hero_level=1,
                attacker=None, last_rating=0, best_stars=0,
                last_seen=None, stats=None, castle=None):
    """Vorschau einer Burg (AttackSelectionService.GetCastleInfo).

    Feldliste aus dem CastleInfo-Leser des Clients. Schwierigkeit,
    Bau-Punkte (Defense Rating), Fallen, Minen und zu erwartende Beute aus
    der Burg und dem Helden-Level (dieselbe Rechnung wie StartAttack);
    Angriffsstatistiken bleiben 0. castle: statt der veröffentlichten Burg
    (delegierte Validierung).
    """
    c = castle or find_castle(castle_id)
    if c is None:
        return None
    s = castle_summary(c)
    ratio = rare_ratio(c)
    creature_loot = loot.creature_loot(c, hero_level, ratio)
    gold = sum(x["Gold"] for x in creature_loot.values())
    lf = sum(x["LifeForce"] for x in creature_loot.values())
    tr_gold, tr_lf = loot.treasure_room(gold, lf, c, hero_level)
    bonus = trophies.attack_bonus(attacker)
    league_gold, league_lf = int(gold * bonus), int(lf * bonus)
    mines = [(f, a) for f, a, storage, _ in loot.stealable_buildings(c, gold, lf, hero_level).values()
             if not storage]
    mine_sum = lambda field: sum(a for f, a in mines if f == field)
    mine_count = lambda field: sum(1 for f, a in mines if f == field)
    traps = sum(len(r.get("Traps") or []) for r in c.get("Rooms") or [])
    trophy_win, trophy_lose, _ = trophies.variation(attacker or {}, c)
    if castle_type(c) == CASTLE_TYPE_USER:
        # Spielerburgen: beste Sterne nur während der Gewinn-Sperre (Original:
        # AttackAssembler.PopulateAccountCastle)
        best_stars = trophies.gain_cooldown(attacker or {}, c.get("AccountId"))[1]
    # Spielerburgen: Siegquote und Zahlen aus castle_stats (ohne Angriffe
    # WinRatioDefaultValue 0.5 = Medium); PVE-Burgen ohne
    st = stats if castle_type(c) == CASTLE_TYPE_USER and stats else {
        "AttackCount": 0, "SuccessfulAttackCount": 0, "WinRatio": 0,
        "WinRatioDifficulty": 0, "AverageDuration": 0}
    win_ratio, win_difficulty = st["WinRatio"], st["WinRatioDifficulty"]
    total_cp = total_construction_points(c)
    # Grenze nach Burg-Level (Herz-Rang = Burg-Level; PVE-Burgen haben kein
    # Herz, Ersatzspieler eins mit Rang 30); nie unter dem Belegten
    max_cp = max(max_construction_points(c, c["Level"]), total_cp)
    return {
        "$type": T("CastleInfo"),
        "AttackabilityStatus": 0,
        "AttackCount": st["AttackCount"],
        "AttackerStatus": 0,
        "AttackSource": attack_source,
        "AttackType": attack_type,
        "AverageDuration": st["AverageDuration"],
        "BestHeroSpecContainerId": 0,
        "CastleType": castle_type(c),
        # "Time to beat": Ganzzahl in ms (timeFromMilliseconds); ein
        # Kommawert in Sekunden ließ das Burgfenster hängen (8d2d9f0)
        "CastleValidationDuration": (int(par_time(c) * 1000)
                                     if castle_type(c) == CASTLE_TYPE_USER else 0),
        "CompletionReward": validation_reward(castle),
        "DefenderAccountSummary": s["DefenderAccountSummary"],
        "DefenderActiveConsumables": [],
        "Difficulty": difficulty(c["Level"], hero_level),
        "FromMachineLearning": False,
        "IsAlmostEmpty": False,
        "IsCastleAttackable": True,
        "IsNew": False,
        "IsShielded": bool(c.get("_ShieldDue")),
        "IsTargetedAttack": (attack_type == 0 and attack_source in (2, 3, 6, 8, 9)
                             and castle_type(c) == CASTLE_TYPE_USER),
        "LastCastleRating": last_rating,
        "LastPublishedDate": "2015-01-01T00:00:00Z",
        "LastSeenOnline": last_seen or "2015-01-01T00:00:00Z",
        "Level": c["Level"],
        "PotentialLoot": {
            "$type": T("PotentialLoot"),
            "BoostIGC": 0, "BoostLifeForce": 0, "BoostXp": 0, "IGC": gold + league_gold,
            "IGCMineCount": mine_count("InGameCoin"), "LeagueBonusIGC": league_gold,
            "LeagueBonusLifeForce": league_lf, "LifeForce": lf + league_lf,
            "LifeForceMineCount": mine_count("LifeForce"),
            "MinesStealableIGC": mine_sum("InGameCoin"),
            "MinesStealableLifeForce": mine_sum("LifeForce"),
            "MinesStealablePremiumCash": mine_sum("PremiumCash"),
            "PremiumCashMineCount": mine_count("PremiumCash"),
            "RareDefenseIngredientRatio": ratio,
            "TreasureRoomStealableIGC": tr_gold, "TreasureRoomStealableLifeForce": tr_lf,
            "Xp": sum(x["Xp"] for x in creature_loot.values()),
        },
        "RevengeAttackId": "",
        "RoomCount": len(c.get("Rooms", []) or []),
        "ShieldExpirationDate": c.get("_ShieldDue") or "2015-01-01T00:00:00Z",
        "Stats": {
            "$type": T("CastleStats"),
            "AttackCount": st["AttackCount"], "HeroesKilled": 0,
            "MaxConstructionPoints": max_cp,
            "SuccessfulAttackCount": st["SuccessfulAttackCount"],
            "TotalConstructionPoints": total_cp,
            "TrapCount": traps,
            # das Burg-Fenster liest Stats.WinRatio/WinRatioDifficulty
            # (CastlePopUpModule.js), nicht die gleichnamigen CastleInfo-Felder
            "WinRatio": win_ratio, "WinRatioDifficulty": win_difficulty,
        },
        "SuccessfulAttackCount": st["SuccessfulAttackCount"],
        "WinRatio": win_ratio, "WinRatioDifficulty": win_difficulty,
        "TrophyScore": trophies.castle_score(c),
        "TrophyScoreVariationLose": trophy_lose,
        "TrophyScoreVariationReason": 0,
        "TrophyScoreVariationWin": trophy_win,
        "VictoryConditionLevel": best_stars,
        "VictoryConditionRewardRatios": [],
    }