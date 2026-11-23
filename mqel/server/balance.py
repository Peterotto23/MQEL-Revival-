"""Tuning knobs of the bling economy (docs/70-bling-wirtschaft.md), editable
in the web tool (Economy page, admin.Get/SetBalance).

Stored in state/<profile>/balance.json. "Enabled" switches the new rules on;
off = original rules, all values stay stored.

Every field has a kind (FIELDS): "server" only affects the server; "data" is
also shown by the client and reaches settings.bin through the patcher (the
DataBin version gets the part B<CRC> of these values; every state stays in
"History" so the patcher can also build deltas from older states, and when
switched off it goes back to the original); "ui" arrives via UI patch
(GameUI package, e.g. achievement blings in the popup).
"""

import copy
import functools
import json
import os
import re
import threading
import zlib
from typing import Any

import settings_archive

SETTINGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "client", "GameData", "Data", "GameplaySettings")
BLING_MINE = 16
BLING_MINE_SKU = "15522"  # shop SKU of the bling mine (original 999 blings)
ACHIEVEMENT_BLINGS_PER_POINT = 2  # default: points x 2 (about 1500 in total)
# Defaults for objective blings (original 50): the seven region bosses pay
# 50, 100, ... 350 in ascending order (+50 per region), Heist Part 2 pays 150;
# 1550 in total instead of 550 (replaces the removed milestones)
# Region order: Snottingham (1-4), Flameo/Firesly (5-9), Durrrr (10-14),
# Clubbage (15-19), Marleybone (20-24), Stabbington (25-29),
# Dragonificus (30-34)
OBJECTIVE_BLINGS = {5: 50, 100: 100, 101: 150, 99: 200, 103: 250, 102: 300,
                    104: 350, 304: 150}
# Repeatable boss raids: castle id (AttackAdvisorSettings BossCastleId) ->
# (boss, default blings per clear). Same order as the objectives above, +10
# per region; paid on top of the one-time quest reward (loot.boss_bounty).
BOSS_CASTLES = {14: ("Count Snottingham", 10), 15: ("Mr. Firesly", 20),
                16: ("Durrrr", 30), 17: ("Captain Clubbage", 40),
                18: ("Lord Marleybone", 50), 19: ("Duke of Stabbington", 60),
                20: ("Dragonificus Terribilus", 70)}


@functools.cache
def achievements():
    """[(Id, DebugName, points)] from AchievementSettings."""
    with open(os.path.join(SETTINGS, "GeneralSettings", "AchievementSettings.json"),
              encoding="utf-8-sig") as f:
        return [(a["Id"], a["DebugName"], a["Points"]) for a in json.load(f)["Achievements"]]


# Bling shop: packs (SpecialPackSettings) and prices, decided in
# docs/analysis/special-packs.md; 67 only during Halloween (bling_shop)
SHOP_PACKS = {1: 1000, 2: 1500, 3: 1500, 4: 1500, 5: 1500, 7: 1500, 8: 1500,
              14: 1500, 15: 300, 26: 800, 27: 800, 28: 800, 45: 1500, 47: 2500,
              49: 300, 50: 1500, 51: 800, 66: 4000, 67: 1500, 69: 300, 71: 1500,
              72: 4000, 74: 2500, 75: 2500}


@functools.cache
def pack_names():
    with open(os.path.join(SETTINGS, "GeneralSettings", "SpecialPackSettings.json"),
              encoding="utf-8-sig") as f:
        return {p["Id"]: p["DebugName"] for p in json.load(f)["SpecialPacks"]}


@functools.cache
def objectives():
    """[(Id, DebugName, original blings)] of the objectives with a bling reward."""
    with open(os.path.join(SETTINGS, "GeneralSettings", "ObjectiveSettings.json"),
              encoding="utf-8-sig") as f:
        out = []
        for o in json.load(f)["Objectives"]:
            for r in (o.get("Reward") or {}).get("RewardItems", []):
                a = r.get("CurrencyAmount") or {}
                if a.get("CurrencyType") == "PremiumCash":
                    out.append((o["Id"], o["DebugName"], a["Amount"]))
        return out


@functools.cache
def fields():
    """{(section, key): {Default, Min, Max, Kind, Label, Help}}."""
    out = {
        # Bling mine (building 16): 1 bling per ProductionHours hours,
        # storage Capacity; lootable for at most StealHours hours of
        # production (at least 1, like PremiumCashMineLossAmount)
        ("Mine", "ProductionHours"): {
            "Default": 4, "Min": 1, "Max": 24, "Kind": "data",
            "Label": "Hours per bling", "Help": "Original: 1"},
        ("Mine", "Capacity"): {
            "Default": 42, "Min": 1, "Max": 1000, "Kind": "data",
            "Label": "Capacity", "Help": "Original: 25"},
        ("Mine", "Price"): {
            "Default": 500, "Min": 0, "Max": 5000, "Kind": "data",
            "Label": "Price (blings)", "Help": "Shop price of a mine. Original: 999"},
        ("Mine", "StealHours"): {
            "Default": 1, "Min": 0, "Max": 168, "Kind": "server",
            "Label": "Lootable hours of production",
            "Help": "At least 1 bling; 0 = not lootable. Original: whole content"},
        # Rare bling loot: with some chance a creature carries Amount blings;
        # at most WeeklyCap from loot in the last 7 days (rolling);
        # WeeklyCap 0 = unlimited
        ("Loot", "Chance"): {
            "Default": 40, "Min": 0, "Max": 100, "Kind": "server",
            "Label": "Chance per attack (%)",
            "Help": "Real attacks only (no practice, tutorial, competition)"},
        ("Loot", "Amount"): {
            "Default": 10, "Min": 1, "Max": 500, "Kind": "server",
            "Label": "Blings per drop", "Help": "Carried by one random creature"},
        ("Loot", "WeeklyCap"): {
            "Default": 0, "Min": 0, "Max": 1000000, "Kind": "server",
            "Label": "Cap per 7 days (0 = unlimited)",
            "Help": "Rolling window, not a fixed week. 0 removes the limit"},
        # Boss raids: every win in a region's boss dungeon pays the boss's
        # amount (repeatable, unlike the one-time quest); at most WeeklyCap
        # from boss raids in the last 7 days (rolling), 0 = unlimited
        **{("Boss", str(cid)): {
            "Default": amount, "Min": 0, "Max": 5000, "Kind": "server",
            "Label": name,
            "Help": "Paid on every win (treasure room reached), 0 = nothing"}
           for cid, (name, amount) in BOSS_CASTLES.items()},
        ("Boss", "WeeklyCap"): {
            "Default": 0, "Min": 0, "Max": 1000000, "Kind": "server",
            "Label": "Cap per 7 days (0 = unlimited)",
            "Help": "All boss raids together; rolling window, not a fixed week"},
        # League (SeasonalCompetitionSettings) and competitions
        # (UbisoftCompetitionSettings): all bling amounts times a percentage
        ("Leagues", "BlingPercent"): {
            "Default": 200, "Min": 0, "Max": 1000, "Kind": "data",
            "Label": "League blings (%)",
            "Help": "All bling rewards of leagues. Original: 100"},
        ("Competitions", "BlingPercent"): {
            "Default": 200, "Min": 0, "Max": 1000, "Kind": "data",
            "Label": "Competition blings (%)",
            "Help": "Top 3 of a competition. Original: 100 (25/10/5)"},
    }
    for pid, price in SHOP_PACKS.items():
        out[("Shop", str(pid))] = {
            "Default": price, "Min": 0, "Max": 20000, "Kind": "server",
            "Label": pack_names()[pid],
            "Help": f"Pack {pid}; 0 = not offered" + (", Halloween only" if pid == 67 else "")}
    for oid, name, amount in objectives():
        out[("Objectives", str(oid))] = {
            "Default": OBJECTIVE_BLINGS.get(oid, amount), "Min": 0, "Max": 5000,
            "Kind": "data", "Label": name, "Help": f"Original: {amount}"}
    for aid, name, points in achievements():
        out[("Achievements", str(aid))] = {
            "Default": points * ACHIEVEMENT_BLINGS_PER_POINT, "Min": 0, "Max": 5000,
            "Kind": "ui", "Label": name, "Help": f"{points} points"}
    return out


def defaults() -> dict[str, Any]:
    values: dict[str, Any] = {"Enabled": False}
    for (section, key), f in fields().items():
        values.setdefault(section, {})[key] = f["Default"]
    return values


_lock = threading.Lock()
_path = None
_data = {"Values": defaults(), "History": {}, "Changed": None}


def load(path):
    """State from path (missing: defaults); unknown keys are dropped."""
    global _path, _data
    _path = path
    values, history, changed = defaults(), {}, None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        values["Enabled"] = bool(stored.get("Values", {}).get("Enabled"))
        for (section, key) in fields():
            v = (stored.get("Values", {}).get(section) or {}).get(key)
            if v is not None:
                values[section][key] = v
        history, changed = stored.get("History", {}), stored.get("Changed")
    _data = {"Values": values, "History": history, "Changed": changed}


def enabled():
    return bool(_data["Values"]["Enabled"])


def get(section, key):
    """Configured value (only takes effect when enabled())."""
    return _data["Values"][section][key]


def _summary():
    return {"Values": copy.deepcopy(_data["Values"]),
            "Fields": [dict(f, Section=s, Key=k) for (s, k), f in fields().items()],
            "ClientVersion": client_version(), "Changed": _data["Changed"]}


def summary():
    with _lock:
        return _summary()


def update(new, now):
    """Apply new values (known fields only, limits checked);
    ValueError with a message for invalid values."""
    with _lock:
        values = copy.deepcopy(_data["Values"])
        if "Enabled" in new:
            values["Enabled"] = bool(new["Enabled"])
        for (section, key), f in fields().items():
            v = (new.get(section) or {}).get(key)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
                raise ValueError(f"{section} {f['Label']} must be a whole number")
            if not f["Min"] <= v <= f["Max"]:
                raise ValueError(
                    f"{section} {f['Label']} must be between {f['Min']} and {f['Max']}")
            values[section][key] = int(v)
        _data["Values"] = values
        _data["Changed"] = now
        cv = _data_values(values)
        if cv is not None:
            _data["History"][_crc(cv)] = cv
        if _path:
            os.makedirs(os.path.dirname(_path), exist_ok=True)
            tmp = _path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_data, f, indent=1)
            os.replace(tmp, _path)
        return _summary()


# --- Client data: settings.bin (patcher, DataBin) -----------------------------

def _data_values(values):
    """Values of kind "data", or None (off = original)."""
    if not values["Enabled"]:
        return None
    out = {f"{section}.{key}": values[section][key]
           for (section, key), f in fields().items() if f["Kind"] == "data"}
    out["Revision"] = DATA_REVISION
    return out


# Fixed data changes while enabled; raising it changes the DataBin version.
# 2: special packs without EarlyHeroAccess (heroes only for blings)
DATA_REVISION = 2


def _crc(cv):
    return f"{zlib.crc32(json.dumps(cv, sort_keys=True).encode()):08x}"


def client_version():
    """Version part "B<CRC>" of the active data values, "" without."""
    cv = _data_values(_data["Values"])
    return "" if cv is None else "B" + _crc(cv)


def history(crc):
    """Data values for a version part (without "B"), or None. The active
    state always counts, even unsaved (e.g. new fields with defaults)."""
    cv = _data_values(_data["Values"])
    if cv is not None and _crc(cv) == crc:
        return cv
    return _data["History"].get(crc)


MINE_FILE = b"BUILDINGS\\000016 - BLINGMINE\\GAMEPLAY.JSON\x00"
SKU_FILE = b"SHOPSETTINGS\\SHOPSKUBASESETTINGS.JSON\x00"
OBJECTIVE_FILE = b"GENERALSETTINGS\\OBJECTIVESETTINGS.JSON\x00"


def _file_span(content, name):
    """(start, end) of the JSON text of the archive file name."""
    start = content.find(name)
    if start < 0 or content.count(name) != 1:
        raise RuntimeError(f"settings.bin: {name!r} not found uniquely")
    begin = start + len(name)
    return begin, content.index(b"\x00", begin)


def _objective_text(text, cv):
    """ObjectiveSettings text with the bling amounts from cv (older states
    in History have none: original)."""
    for oid, _, original in objectives():
        head = re.compile(rb'"Id":%d,\s*"DebugName"' % oid)
        heads = list(head.finditer(text))
        if len(heads) != 1:
            raise RuntimeError(f"settings.bin: objective {oid} not unique")
        amount = re.compile(rb'("CurrencyType":"PremiumCash",\s*"Amount":)(\d+)')
        m = amount.search(text, heads[0].end())
        nxt = text.find(b'"DebugName"', heads[0].end())
        if m is None or (nxt >= 0 and m.start() > nxt):
            raise RuntimeError(f"settings.bin: blings of objective {oid} missing")
        text = (text[:m.start(2)] + b"%d" % cv.get(f"Objectives.{oid}", original)
                + text[m.end(2):])
    return text


LEAGUE_FILE = b"GENERALSETTINGS\\SEASONALCOMPETITIONSETTINGS.JSON\x00"
COMPETITION_FILE = b"GENERALSETTINGS\\UBISOFTCOMPETITIONSETTINGS.JSON\x00"
PACK_FILE = b"GENERALSETTINGS\\SPECIALPACKSETTINGS.JSON\x00"
EARLY_HEROES = re.compile(rb',\s*"EarlyHeroAccess":\s*\[[^\]]*\]')
BLING_AMOUNT = re.compile(rb'("CurrencyType":(?:"PremiumCash"|1),\s*"Amount":)(\d+)')


def _mine_text(text, cv):
    for old, new in ((b'"ProductionPeriod":"1h"',
                      b'"ProductionPeriod":"%dh"' % cv["Mine.ProductionHours"]),
                     (b'"Capacity":25,', b'"Capacity":%d,' % cv["Mine.Capacity"])):
        if text.count(old) != 1:
            raise RuntimeError(f"settings.bin: {old!r} not unique")
        text = text.replace(old, new)
    return text


def _sku_text(text, cv):
    """Shop price of the bling mine (older states without the value: original)."""
    if "Mine.Price" not in cv:
        return text
    sku = re.compile(rb'"Code":"%s",.*?"Amount":(\d+)' % BLING_MINE_SKU.encode(), re.DOTALL)
    m = sku.search(text)
    if m is None or b'"Code"' in text[m.start() + 1:m.start(1)]:
        raise RuntimeError("settings.bin: price of the bling mine not found")
    return text[:m.start(1)] + b"%d" % cv["Mine.Price"] + text[m.end(1):]


def _percent_text(key):
    """Editor: all bling amounts times cv[key] percent (older states
    without the value: original)."""
    def edit(text, cv):
        pct = cv.get(key, 100)
        if not BLING_AMOUNT.search(text):
            raise RuntimeError(f"settings.bin: no blings found for {key}")
        return BLING_AMOUNT.sub(lambda m: m.group(1) + b"%d" % scaled(int(m.group(2)), pct),
                                text)
    return edit


def _pack_text(text, cv):
    """Special packs without early hero access (from revision 2)."""
    if cv.get("Revision", 1) < 2:
        return text
    if not EARLY_HEROES.search(text):
        raise RuntimeError("settings.bin: EarlyHeroAccess not found")
    return EARLY_HEROES.sub(b"", text)


def settings_bin(original, cv):
    """settings.bin with the data values cv (bling mine, objective, league and
    competition blings). Repacked from the first changed file on."""
    content, _, _ = settings_archive.read(original)
    edits = sorted((_file_span(content, name), fn) for name, fn in (
        (MINE_FILE, _mine_text), (SKU_FILE, _sku_text), (OBJECTIVE_FILE, _objective_text),
        (LEAGUE_FILE, _percent_text("Leagues.BlingPercent")),
        (COMPETITION_FILE, _percent_text("Competitions.BlingPercent")),
        (PACK_FILE, _pack_text)))
    first = edits[0][0][0]
    tail, pos = b"", first
    for (begin, end), fn in edits:
        tail += content[pos:begin] + fn(content[begin:end], cv)
        pos = end
    return settings_archive.replace_packed(original, first, tail + content[pos:-4])


def mine_rank(spec_id, spec):
    """MineBuildingRankSpec of the bling mine with the configured values."""
    if spec is None or spec_id != BLING_MINE or not enabled():
        return spec
    return dict(spec, ProductionPeriod=f"{get('Mine', 'ProductionHours')}h",
                ProductionValue=1, Capacity=get("Mine", "Capacity"))


def sku_price(sku):
    """Price of the SKU ({CurrencyType, Amount} or None), bling mine with the
    configured price."""
    price = sku.get("Price")
    if sku["Code"] == BLING_MINE_SKU and enabled():
        return dict(price, Amount=get("Mine", "Price"))
    return price


# --- Achievements ------------------------------------------------------------

def achievement_blings(achievement_id):
    """Blings for the achievement (0 when off)."""
    if not enabled():
        return 0
    return _data["Values"]["Achievements"].get(str(achievement_id), 0)


def ui_achievement_blings():
    """{Id: blings} for the UI patch (empty when off; without zeros)."""
    if not enabled():
        return {}
    return {int(k): v for k, v in _data["Values"]["Achievements"].items() if v}


# --- Objectives --------------------------------------------------------------

def objective_blings(objective_id, original):
    """Blings for the objective (original when off or without a field)."""
    if not enabled():
        return original
    return _data["Values"].get("Objectives", {}).get(str(objective_id), original)


# --- Leagues and competitions ------------------------------------------------

def scaled(amount, percent):
    return int(amount * percent / 100 + 0.5)


def scale_reward(reward, section):
    """Copy of reward (RewardItems) with bling amounts times
    section.BlingPercent; unchanged when off."""
    if not reward or not enabled():
        return reward
    pct = get(section, "BlingPercent")
    out = copy.deepcopy(reward)
    for r in out.get("RewardItems") or []:
        a = r.get("CurrencyAmount")
        if a and a.get("CurrencyType") in ("PremiumCash", 1):
            a["Amount"] = scaled(a["Amount"], pct)
    return out


# --- Bling shop --------------------------------------------------------------

def shop_prices():
    """{pack id: blings} of the offered packs (empty when off)."""
    if not enabled():
        return {}
    return {int(k): v for k, v in _data["Values"]["Shop"].items() if v}