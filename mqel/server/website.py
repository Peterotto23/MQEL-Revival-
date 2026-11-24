"""Public project page (website/, game_server --web-port): blings.html is
generated from server/data/blings.html with the values of the bling economy,
at startup and after every save of the Economy page."""

import math
import os

import balance
from account_state import ROOT

TEMPLATE = os.path.join(ROOT, "server", "data", "blings.html")
PAGE = os.path.join(ROOT, "website", "blings.html")
COMPETITION_TOP3 = (25, 10, 5)  # UbisoftCompetitionSettings, original


def _n(v):
    return f"{v:,}"


def _times(pct):
    return {100: "The original rewards", 200: "Twice the original rewards",
            300: "Three times the original rewards"}.get(pct, f"{pct}% of the original rewards")


def _fill_time(hours):
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
    prices = [p for p in (g("Shop", str(pid)) for pid in balance.SHOP_PACKS) if p]
    comp = g("Competitions", "BlingPercent")
    boss = [g("Boss", str(cid)) for cid in balance.BOSS_CASTLES]
    return {
        "quests": _n(sum(quests)), "quest_max": _n(max(quests)),
        "achievements": _n(sum(g("Achievements", str(aid))
                               for aid, _, _ in balance.achievements())),
        "mine_rate": _n(rate), "mine_capacity": _n(cap),
        "mine_fill": _fill_time(cap / rate),
        "mine_steal": ("Attackers can't steal from it." if not steal else
                       "A successful attack steals only 1 bling." if steal_blings <= 1 else
                       f"A successful attack steals up to {_n(steal_blings)} blings."),
        "loot_amount": g("Loot", "Amount"), "loot_chance": g("Loot", "Chance"),
        "loot_how": _loot_how(g("Loot", "Chance"), g("Loot", "PvP"), g("Loot", "PvE")),
        "loot_cap": _n(g("Loot", "WeeklyCap")),
        "loot_limit": ("No weekly limit." if g("Loot", "WeeklyCap") <= 0 else
                       f"Up to {_n(g('Loot', 'WeeklyCap'))} in any 7 days."),
        "boss_min": _n(min(boss, default=0)), "boss_max": _n(max(boss, default=0)),
        "boss_limit": ("No weekly limit." if g("Boss", "WeeklyCap") <= 0 else
                       f"Up to {_n(g('Boss', 'WeeklyCap'))} in any 7 days."),
        "leagues": _times(g("Leagues", "BlingPercent")),
        "competitions": " / ".join(str(balance.scaled(a, comp)) for a in COMPETITION_TOP3),
        "shop_min": _n(min(prices, default=0)), "shop_max": _n(max(prices, default=0)),
    }


def write():
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read().format(**values())
    tmp = PAGE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, PAGE)