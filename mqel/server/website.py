"""Public project page (website/, game_server --web-port): blings.html is
generated from server/data/blings.html with the values of the bling economy,
at startup and after every save of the Economy page."""

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


def values():
    g = balance.get
    quests = [g("Objectives", str(oid)) for oid, _, _ in balance.objectives()]
    hours, cap = g("Mine", "ProductionHours"), g("Mine", "Capacity")
    days = round(hours * cap / 24)
    steal = g("Mine", "StealHours")
    prices = [p for p in (g("Shop", str(pid)) for pid in balance.SHOP_PACKS) if p]
    comp = g("Competitions", "BlingPercent")
    boss = [g("Boss", str(cid)) for cid in balance.BOSS_CASTLES]
    return {
        "quests": _n(sum(quests)), "quest_max": _n(max(quests)),
        "achievements": _n(sum(g("Achievements", str(aid))
                               for aid, _, _ in balance.achievements())),
        "mine_hours": hours, "mine_capacity": cap,
        "mine_days": "a week" if days == 7 else f"{days} days",
        "mine_steal": ("Attackers can't steal from it." if not steal else
                       "A successful attack steals only 1 bling." if steal <= hours else
                       f"A successful attack steals up to {steal // hours} blings."),
        "loot_amount": g("Loot", "Amount"), "loot_chance": g("Loot", "Chance"),
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