#!/usr/bin/env python3
"""Bling economy patch: mine as "blings per hour" (range up to 1000) and a
PvP / PvE switch for the rare creature bling drops.

Run from the repo root (the folder that contains server/, tests/, docs/):

    python3 apply_bling_patch.py            # apply
    python3 apply_bling_patch.py --check    # only test that every edit matches

All edits are computed in memory first. If any one of them does not match
(wrong file version), nothing is written. Each file keeps its own line
endings (balance.py is LF, the others CRLF). Running it twice is a no-op.
"""

import os
import sys

# (path, old, new, expected matches)
EDITS = []


def E(path, old, new, count=1):
    EDITS.append((path, old, new, count))


# ---------------------------------------------------------------- balance.py
B = "server/balance.py"
E(B, '''        # Bling mine (building 16): 1 bling per ProductionHours hours,
        # storage Capacity; lootable for at most StealHours hours of
        # production (at least 1, like PremiumCashMineLossAmount)
        ("Mine", "ProductionHours"): {
            "Default": 4, "Min": 1, "Max": 24, "Kind": "data",
            "Label": "Hours per bling", "Help": "Original: 1"},''',
  '''        # Bling mine (building 16): BlingsPerHour blings every hour (the
        # production period stays 1 h, ProductionValue carries the rate),
        # storage Capacity; lootable for at most StealHours hours of
        # production (0 = not lootable)
        ("Mine", "BlingsPerHour"): {
            "Default": 50, "Min": 1, "Max": 1000, "Kind": "data",
            "Label": "Blings per hour", "Help": "Original: 1"},''')

E(B, '''        # Rare bling loot: with some chance a creature carries Amount blings;
        # at most WeeklyCap from loot in the last 7 days (rolling);
        # WeeklyCap 0 = unlimited
        ("Loot", "Chance"): {
            "Default": 40, "Min": 0, "Max": 100, "Kind": "server",
            "Label": "Chance per attack (%)",
            "Help": "Real attacks only (no practice, tutorial, competition)"},''',
  '''        # Rare bling loot: with some chance a creature carries Amount blings;
        # at most WeeklyCap from loot in the last 7 days (rolling);
        # WeeklyCap 0 = unlimited. PvP / PvE choose by castle type: player
        # castles (CastleType User, incl. the bot players) are PvP, the
        # Ubisoft campaign castles (incl. boss dungeons) are PvE.
        ("Loot", "Chance"): {
            "Default": 40, "Min": 0, "Max": 100, "Kind": "server",
            "Label": "Chance per attack (%)",
            "Help": "Real attacks only (no practice, tutorial, competition)"},
        ("Loot", "PvP"): {
            "Default": 1, "Min": 0, "Max": 1, "Kind": "server",
            "Label": "Drops in player castles (PvP)",
            "Help": "1 = on, 0 = off. Player castles, bot players included"},
        ("Loot", "PvE"): {
            "Default": 0, "Min": 0, "Max": 1, "Kind": "server",
            "Label": "Drops in campaign castles (PvE)",
            "Help": "1 = on, 0 = off. Campaign castles; boss raids pay via the Boss values"},''')

# range of blings per drop: 500 -> 1000
E(B, '''        ("Loot", "Amount"): {
            "Default": 10, "Min": 1, "Max": 500, "Kind": "server",''',
  '''        ("Loot", "Amount"): {
            "Default": 10, "Min": 1, "Max": 1000, "Kind": "server",''')

E(B, '''def _mine_text(text, cv):
    # Match the number, not the original value: the Steam settings.bin has Capacity 42
    # (the loose 2015 JSON says 25), and the hours may already differ from 1.
    for pattern, new in ((rb'"ProductionPeriod":"\\d+h"',
                          b'"ProductionPeriod":"%dh"' % cv["Mine.ProductionHours"]),
                         (rb'"Capacity":\\d+', b'"Capacity":%d' % cv["Mine.Capacity"])):''',
  '''def _mine_rate(cv):
    """(period hours, blings per period) of the bling mine in cv. Older states
    in History have Mine.ProductionHours instead (1 bling per N hours)."""
    if "Mine.BlingsPerHour" in cv:
        return 1, cv["Mine.BlingsPerHour"]
    return cv["Mine.ProductionHours"], 1


def _mine_text(text, cv):
    # Match the number, not the original value: the Steam settings.bin has Capacity 42
    # (the loose 2015 JSON says 25), and period/value may already differ from 1h / 1.
    hours, value = _mine_rate(cv)
    for pattern, new in ((rb'"ProductionPeriod":"\\d+h"', b'"ProductionPeriod":"%dh"' % hours),
                         (rb'"ProductionValue":\\d+', b'"ProductionValue":%d' % value),
                         (rb'"Capacity":\\d+', b'"Capacity":%d' % cv["Mine.Capacity"])):''')

E(B, '''    return dict(spec, ProductionPeriod=f"{get('Mine', 'ProductionHours')}h",
                ProductionValue=1, Capacity=get("Mine", "Capacity"))''',
  '''    return dict(spec, ProductionPeriod="1h",
                ProductionValue=get("Mine", "BlingsPerHour"),
                Capacity=get("Mine", "Capacity"))''')

# -------------------------------------------------------------------- loot.py
L = "server/loot.py"
E(L, '''                        steal = balance.get("Mine", "StealHours")
                        steal = steal and max(1, steal // balance.get("Mine", "ProductionHours"))
                        amount = min(amount, steal)''',
  '''                        # StealHours of production (0 = not lootable); the
                        # rate is at least 1 per hour, so at least 1 bling
                        steal = balance.get("Mine", "StealHours") * balance.get("Mine", "BlingsPerHour")
                        amount = min(amount, steal)''')

E(L, '''def add_bling_drop(creature_loot, account, now, rng=random):
    """With Loot.Chance, puts the blings on a random creature
    (DefenseIngredientLoot.PremiumCash); capped to the remaining budget."""
    if not balance.enabled() or not creature_loot:
        return
''',
  '''def bling_drop_allowed(pvp):
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
''')

# ------------------------------------------------------------------ attack.py
E("server/attack.py",
  '''        loot.add_bling_drop(creature_loot, account, time.time())''',
  '''        # PvP = player castle (incl. the bot players), PvE = campaign castle
        loot.add_bling_drop(creature_loot, account, time.time(),
                            pvp=castles.castle_type(c) == castles.CASTLE_TYPE_USER)''')

# ----------------------------------------------------------------- website.py
W = "server/website.py"
E(W, '''import os

import balance''', '''import math
import os

import balance''')
E(W, '''def values():
    g = balance.get
    quests = [g("Objectives", str(oid)) for oid, _, _ in balance.objectives()]
    hours, cap = g("Mine", "ProductionHours"), g("Mine", "Capacity")
    days = round(hours * cap / 24)
    steal = g("Mine", "StealHours")
''', '''def _fill_time(hours):
    """Readable time until the mine is full."""
    hours = max(1, math.ceil(hours))
    if hours < 48:
        return f"{hours} hour" + ("" if hours == 1 else "s")
    days = round(hours / 24)
    return "a week" if days == 7 else f"{days} days"


def _loot_how(chance, pvp, pve):
    """Sentence about where the rare creature loot drops."""
    if pvp and pve:
        return f"{chance}% chance per attack that one creature carries blings."
    if pvp or pve:
        kind = "a player castle" if pvp else "a campaign castle"
        return f"{chance}% chance per attack on {kind} that one creature carries blings."
    return "Currently switched off."


def values():
    g = balance.get
    quests = [g("Objectives", str(oid)) for oid, _, _ in balance.objectives()]
    rate, cap = g("Mine", "BlingsPerHour"), g("Mine", "Capacity")
    steal = g("Mine", "StealHours")
    steal_blings = steal * rate
''')
E(W, '''        "mine_hours": hours, "mine_capacity": cap,
        "mine_days": "a week" if days == 7 else f"{days} days",
        "mine_steal": ("Attackers can't steal from it." if not steal else
                       "A successful attack steals only 1 bling." if steal <= hours else
                       f"A successful attack steals up to {steal // hours} blings."),
        "loot_amount": g("Loot", "Amount"), "loot_chance": g("Loot", "Chance"),''',
  '''        "mine_rate": _n(rate), "mine_capacity": _n(cap),
        "mine_fill": _fill_time(cap / rate),
        "mine_steal": ("Attackers can't steal from it." if not steal else
                       "A successful attack steals only 1 bling." if steal_blings <= 1 else
                       f"A successful attack steals up to {_n(steal_blings)} blings."),
        "loot_amount": g("Loot", "Amount"), "loot_chance": g("Loot", "Chance"),
        "loot_how": _loot_how(g("Loot", "Chance"), g("Loot", "PvP"), g("Loot", "PvE")),''')

T = "server/data/blings.html"
E(T, '''<td class="n">1 per {mine_hours} h</td><td>Holds up to {mine_capacity}, about {mine_days}'s worth. {mine_steal}</td>''',
  '''<td class="n">{mine_rate} per hour</td><td>Holds up to {mine_capacity}, full after {mine_fill}. {mine_steal}</td>''')
E(T, '''<td>{loot_chance}% chance per attack that one creature carries blings. {loot_limit}</td>''',
  '''<td>{loot_how} {loot_limit}</td>''')

# ------------------------------------------------------------------ the tests
TS = "tests/test_server.py"
E(TS, '''        self.assertEqual((spec["ProductionPeriod"], spec["Capacity"]), ("4h", 42))
        self.assertEqual(loot.stealable_buildings(self.mine_castle(20), 0, 0)[7][1], 1)
        balance.update({"Mine": {"ProductionHours": 1, "StealHours": 3}}, "x")''',
  '''        self.assertEqual(
            (spec["ProductionPeriod"], spec["ProductionValue"], spec["Capacity"]),
            ("1h", 50, 42),
        )
        # one hour of production (50) is lootable, not the whole fill level
        self.assertEqual(loot.stealable_buildings(self.mine_castle(200), 0, 0)[7][1], 50)
        balance.update({"Mine": {"BlingsPerHour": 1, "StealHours": 3}}, "x")''')
E(TS, '''        self.assertIn(b'"Capacity":42,', content[i : i + 3000])''',
  '''        self.assertIn(b'"Capacity":42,', content[i : i + 3000])
        self.assertRegex(content[i : i + 3000], rb'"ProductionValue":1\\b')''')
E(TS, '''    def test_bling_loot(self):
''', '''    def test_mine_rate_and_ranges(self):
        balance.update(
            {"Enabled": True, "Mine": {"BlingsPerHour": 1000, "Capacity": 1000}}, "x"
        )
        spec = castles.mine_rank(16, 1)
        self.assertEqual(
            (spec["ProductionPeriod"], spec["ProductionValue"]), ("1h", 1000)
        )
        for bad in (0, 1001):
            with self.assertRaises(ValueError):
                balance.update({"Mine": {"BlingsPerHour": bad}}, "x")
        balance.update({"Loot": {"Amount": 1000}}, "x")  # up to 1000 per drop
        with self.assertRaises(ValueError):
            balance.update({"Loot": {"Amount": 1001}}, "x")
        # states saved before the change had hours per bling
        self.assertEqual(balance._mine_rate({"Mine.ProductionHours": 4}), (4, 1))
        self.assertEqual(balance._mine_rate({"Mine.BlingsPerHour": 50}), (1, 50))
        # lootable = StealHours x rate; 0 hours = not lootable
        balance.update({"Mine": {"BlingsPerHour": 50, "StealHours": 1}}, "x")
        self.assertEqual(loot.stealable_buildings(self.mine_castle(400), 0, 0)[7][1], 50)
        self.assertEqual(loot.stealable_buildings(self.mine_castle(20), 0, 0)[7][1], 20)
        balance.update({"Mine": {"StealHours": 0}}, "x")
        self.assertEqual(loot.stealable_buildings(self.mine_castle(400), 0, 0)[7][1], 0)

    def test_bling_loot_pvp_pve(self):
        account, now = {}, 1_000_000
        balance.update(
            {"Enabled": True, "Loot": {"Chance": 100, "Amount": 10, "WeeklyCap": 0}}, "x"
        )

        def dropped(pvp):
            table = {1: {}, 2: {}}
            loot.add_bling_drop(table, account, now, pvp=pvp)
            return sum(x.get("PremiumCash", 0) for x in table.values())

        self.assertEqual(dropped(True), 10)  # default: player castles drop
        self.assertEqual(dropped(False), 0)  # default: campaign castles do not
        self.assertEqual(dropped(None), 10)  # kind unknown: allowed
        balance.update({"Loot": {"PvP": 0, "PvE": 1}}, "x")
        self.assertEqual((dropped(True), dropped(False)), (0, 10))
        balance.update({"Loot": {"PvP": 1, "PvE": 1}}, "x")
        self.assertEqual((dropped(True), dropped(False)), (10, 10))
        balance.update({"Loot": {"PvP": 0, "PvE": 0}}, "x")
        self.assertEqual((dropped(True), dropped(False)), (0, 0))
        with self.assertRaises(ValueError):
            balance.update({"Loot": {"PvP": 2}}, "x")
        # castle kinds as attack.start_attack tells them apart
        self.assertEqual(
            castles.castle_type({"_CastleType": castles.CASTLE_TYPE_USER}),
            castles.CASTLE_TYPE_USER,
        )
        self.assertEqual(castles.castle_type({}), castles.CASTLE_TYPE_UBISOFT)

    def test_bling_loot(self):
''')

# ------------------------------------------------------------------- the docs
D = "docs/70-bling-wirtschaft.md"
E(D, '''- Production 1/h, storage 42 (full after 42 h): deliberately rewards players who
  collect every 1–2 days (~150–168 per week); weekly only 42 (decided
  2026-11-09).''',
  '''- Production 50/h (`Mine.BlingsPerHour`, range 1–1000; community poll
  2026-11-24, catching up with the boss raids), storage per the poll 400 (full
  after 8 h; `Mine.Capacity`), one hour of production (50) lootable. Earlier
  state (decided 2026-11-09): 1 bling per 4 h, storage 42.
- The period stays 1 h, the rate is carried by `ProductionValue`, so the
  client shows and the server pays the same number. States saved with
  `Mine.ProductionHours` stay readable in `History` (`balance._mine_rate`).''')
E(D, '''- **Bling mine:** when Enabled, castles.mine_rank delivers ProductionPeriod/
  Capacity from balance; loot.stealable_buildings limits the loot to
  StealHours of production (minimum 1).''',
  '''- **Bling mine:** when Enabled, castles.mine_rank delivers ProductionPeriod
  (1 h), ProductionValue (BlingsPerHour) and Capacity from balance;
  loot.stealable_buildings limits the loot to StealHours x BlingsPerHour
  (0 hours = not lootable).''')
E(D, '''  `Loot.Amount` Blings (default 10; DefenseIngredientLoot.PremiumCash).''',
  '''  `Loot.Amount` Blings (default 10, up to 1000; DefenseIngredientLoot.PremiumCash).
  Where it drops is chosen by castle type: `Loot.PvP` (default on) = player
  castles incl. the bot players (CastleType User), `Loot.PvE` (default off) =
  campaign castles incl. boss dungeons, which pay via `Boss.<castle id>`
  instead (`attack.start_attack` passes `pvp=` to `loot.add_bling_drop`).''')


# ----------------------------------------------------------------- machinery
def apply(root, write=True):
    files = {}
    for path, old, new, count in EDITS:
        full = os.path.join(root, *path.split("/"))
        if path not in files:
            with open(full, "rb") as f:
                files[path] = f.read().decode("utf-8")
        data = files[path]
        if "\r\n" in data:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        n = data.count(old)
        if n != count:
            sys.exit(f"{path}: expected {count} match(es), found {n} for:\n{old[:140]!r}\n"
                     "Nothing was written.")
        files[path] = data.replace(old, new)
    if write:
        for path, data in files.items():
            with open(os.path.join(root, *path.split("/")), "wb") as f:
                f.write(data.encode("utf-8"))
    return sorted(files)


def main():
    root = os.getcwd()
    balance = os.path.join(root, "server", "balance.py")
    if not os.path.exists(balance):
        sys.exit("Run this from the repo root (the folder with server/, tests/, docs/).")
    with open(balance, encoding="utf-8") as f:
        if "BlingsPerHour" in f.read():
            print("Already applied, nothing to do.")
            return
    check = "--check" in sys.argv
    touched = apply(root, write=not check)
    print(("All edits match: " if check else "Patched: ") + ", ".join(touched))


if __name__ == "__main__":
    main()