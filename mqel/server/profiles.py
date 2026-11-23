"""Öffentliche Spielerprofile (AccountInformationService.GetPublicProfileById).

Felder aus dem AccountPublicProfile-Leser des Clients (0xa89ff0).
"""

import functools
import json
import os
from typing import Any

import achievements as ach_rules
import castles
import loot
import trophies

T = castles.T


def fake_account(account_id):
    """Account eines Dummy-Spielers (castles.FAKE_PLAYER_BASE + Burg-Id)
    aus seiner Burg, oder None. Keine Helden und keine Statistik; Name,
    Burg-Level und Kronen wie in der Burgvorschau."""
    if (account_id < castles.FAKE_PLAYER_BASE
            or castles.player_account(account_id) is not None):
        return None
    c = castles.find_castle(account_id)
    if c is None:
        return None
    return {"AccountId": account_id, "DisplayName": c["AccountDisplayName"],
            "CountryCode": c.get("CountryCode", ""),
            "TrophyScore": trophies.castle_score(c), "IsFake": True,
            "BuildInfo": {"Level": c.get("Level", 1), "Draft": c,
                          "CastleStats": {"$type": T("CastleStats")}},
            "Stats": {"$type": T("AccountStats")}, "Heroes": []}


def account_summary(a, guild_header=None):
    """AccountSummary eines Spielers (auch für Clan-Mitglieder)."""
    league, sub = trophies.account_league(a)
    return {"$type": T("AccountSummary"), "Id": a["AccountId"],
            "DisplayName": a.get("DisplayName", ""),
            "AvatarId": a.get("AvatarId", 10),
            "CastleLevel": a.get("BuildInfo", {}).get("Level", 1),
            "CountryCode": a.get("CountryCode", ""),
            "GuildHeader": guild_header,
            # immer (ohne validierte Burg: Ersatzburg, castles.player_castle)
            "IsCastleAttackable": True,
            "LeagueId": league, "OasisNameId": 0,
            "SpecialPacks": [], "SubLeagueId": sub,
            "TrophyScore": a.get("TrophyScore", 0)}


def search(query, max_result, accounts, guild_header):
    """ProfileSearchResult (AccountInformationService.Search): Spieler und
    Ersatzspieler, deren Anzeigename query enthält (ohne Groß-/Kleinschreibung),
    nach Namen sortiert, höchstens max_result. Einträge als AccountSummary
    wie in der Freundesliste (der Client liest sie mit SpecialPacks)."""
    q = (query or "").strip().casefold()
    found = []
    if q:
        fakes = (fake_account(c["AccountId"]) for c in castles.fake_player_castles())
        for a in [a for a in accounts if a.get("DisplayName")] + [f for f in fakes if f]:
            if q in a["DisplayName"].casefold():
                found.append(a)
    found.sort(key=lambda a: a["DisplayName"].casefold())
    return {"$type": T("ProfileSearchResult"), "MaxResult": max_result,
            "Query": query or "",
            "Results": [account_summary(a, guild_header(a["AccountId"]))
                        for a in found[:max_result]]}


ACHIEVEMENT_TYPES = ["None", "DefeatCastle", "KillEntities", "ReachLevel",
                     "BuildEntities", "KillHeroes", "ChallengeParticipants",
                     "FriendsCount", "CurrencyAccumulation", "Equipment",
                     "DrinkPotion", "DefeatCastleStrike", "LootItems",
                     "CastlePillaged"]


@functools.cache
def achievement_specs():
    path = os.path.join(loot.SETTINGS, "GeneralSettings", "AchievementSettings.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["Achievements"]


@functools.cache
def _texts():
    """Englische Oasis-Texte (Titel/Beschreibung der Errungenschaften)."""
    path = os.path.join(loot.SETTINGS, "Oasis", "oasis_en.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["Translations"]


def achievements(a):
    """AchievementContainer: alle Errungenschaften aus AchievementSettings,
    erledigte (CompletedAchievements) mit 100 %, sonst der Fortschritt
    aus der Statistik (achievements.progress)."""
    done = set(a.get("CompletedAchievements") or [])
    out = []
    for spec in achievement_specs():
        ach = dict(spec)
        name = spec["$type"].split(",")[0].split(".")[-1].replace("Achievement", "")
        ach.update(Type=ACHIEVEMENT_TYPES.index(name) if name in ACHIEVEMENT_TYPES else 0,
                   Title=_texts().get(str(spec.get("TitleId")), ""),
                   Description=_texts().get(str(spec.get("DescriptionId")), ""))
        is_done = spec["Id"] in done
        out.append({"$type": T("AchievementProgression"), "Achievement": ach,
                    "CompletionDate": ach_rules.completion_date(a, spec["Id"]) if is_done else None,
                    "CompletionDateFriendly": "",
                    "CompletionPercentage": 100 if is_done else _percent(a, spec)})
    return {"$type": T("AchievementContainer"), "Achievements": out,
            "CompletedCount": len(done), "TotalCount": len(out)}


def _percent(a, spec):
    got, need = ach_rules.progress(a, spec)
    return min(99, got * 100 // need) if need else 0


def public_profile(a, guild_header=None):
    build = a.get("BuildInfo", {})
    draft = build.get("Draft") or {}
    hero = next((h for h in a.get("Heroes") or []
                 if h["HeroSpecContainerId"] == a.get("SelectedHeroId")),
                (a.get("Heroes") or [{}])[0])
    stats: dict[str, Any] = dict(build.get("CastleStats") or {"$type": T("CastleStats")})
    stats.update(TotalConstructionPoints=castles.total_construction_points(draft),
                 MaxConstructionPoints=castles.max_construction_points(draft, build.get("Level", 1)),
                 TrapCount=sum(len(r.get("Traps") or []) for r in draft.get("Rooms") or []))
    summary = account_summary(a, guild_header)
    return {
        "$type": T("AccountPublicProfile"),
        "AccountStats": a.get("Stats"),
        "AccountSummary": summary,
        "Achievements": achievements(a),
        "CastleLevel": build.get("Level", 1),
        "CastleStats": stats,
        "GamerScore": a.get("GamerScore", 0),
        "Heroes": a.get("Heroes") or [],
        "HeroLevel": hero.get("Level", 1),
        # steuert den Angriffsknopf im Profil
        "IsCastleAttackable": summary["IsCastleAttackable"],
        "IsShielded": castles.shield_due(a) is not None,
        "LeagueId": trophies.account_league(a)[0],
        "SelectedHeroId": a.get("SelectedHeroId", 0),
        "TrophyScore": a.get("TrophyScore", 0),
    }