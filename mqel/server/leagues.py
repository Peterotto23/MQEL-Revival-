"""Ligen, Saisons und Bestenlisten (SeasonalCompetitionService).

Wie im Original (SeasonalCompetitionManager): Die Liga eines Accounts gilt
eine ganze Saison. Beim ersten Kontakt nach dem Saisonende
(GetAccountInformation, GetSeasonalCompetition,
CheckSeasonalCompetitionRewards) wird abgerechnet (settle): neue Liga aus
den Kronen (trophies.league_of, Stufen aus den
SeasonalCompetitionSettings), bei gleicher Liga LeagueMaintainReward, bei
höherer LeagueReachedReward, LeagueUpdated-Meldung und -News. Das Ergebnis
zeigt der Client einmal als Saisonende-Fenster (PreviousLeagueInfo in der
nächsten LeaderboardPage). Die Bestenliste umfasst die echten Spieler und
die Ersatzspieler (Liga nach Kronen); Antwort ist eine LeaderboardPage
(Felder laut Client-Leser).

Abfrage des Clients: level (LeaderboardLevel: World 1, Zone 2, Country 3,
Leagues 4, Friends 5), leagueId/subLeagueId (0 = eigene), geoInfo
(Länder-/Zonencode), seekMode (LeaderboardSeekMode: First 1, MyRank 2,
Previous 3, Next 4, Last 5), refScore/refSeconds/refAccountId (Eintrag,
von dem aus geblättert wird), pageSize, returnGeoFilters.

Saison: ReferenceDate + ClosingPeriod * ClosingPeriodCount (Live: 7 Tage).
Dazu täglich (ClosingPeriod) die Belohnungen der Ranglisten-Ersten
(settle).
"""

import datetime
import functools
import re
from typing import Any

import balance
import castles
import objectives
import profiles
import trophies

T = castles.T

# Account-Felder (nur serverseitig): Beginn der zuletzt abgerechneten
# Saison; Ergebnis der letzten Saison {LeagueId, SubLeagueId,
# LeagueRank, WorldRank, Viewed}
SEASON_START = "SeasonStart"
PERIOD_START = "LeaguePeriodStart"  # zuletzt abgerechnete Tageswertung
PREVIOUS_SEASON = "PreviousSeason"
# NewsType (Enum des Clients)
NEWS_LEAGUE_UPDATED, NEWS_LEAGUE_RANK_REACHED, NEWS_WORLD_RANK_REACHED = 18, 19, 20

WORLD, ZONE, COUNTRY, LEAGUES, FRIENDS = 1, 2, 3, 4, 5
FIRST, MY_RANK, PREVIOUS, NEXT, LAST = 1, 2, 3, 4, 5

_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def _seconds(span):
    """TimeSpan der Client-Daten ("1d", "15m") in Sekunden."""
    return sum(int(n) * _UNITS[u.lower()] for n, u in re.findall(r"(\d+)([wdhmsWDHMS])", span))


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _season_length():
    plan = trophies.league_settings()["ScheduleConfigs"][0]
    return (_seconds(plan["ClosingPeriod"])
            * plan["SeasonalRewardConfig"]["ClosingPeriodCount"])


def season_end(now=None):
    """Ende der laufenden Saison (Live-Plan)."""
    plan = trophies.league_settings()["ScheduleConfigs"][0]
    ref = datetime.datetime.fromisoformat(plan["ReferenceDate"])
    now = now or datetime.datetime.now(datetime.UTC)
    done = int((now - ref).total_seconds()) // _season_length() + 1
    return ref + datetime.timedelta(seconds=done * _season_length())


def season_start(now=None):
    """Beginn der laufenden Saison als ISO-Text."""
    return _iso(season_end(now) - datetime.timedelta(seconds=_season_length()))


def _sub_league(league):
    for lg in trophies.league_settings()["Leagues"]:
        for sub in lg["SubLeagues"]:
            if (lg["Id"], sub["Id"]) == league:
                return sub
    return {}


def _ranked(accounts, by_league):
    """Accounts nach Kronen absteigend (bei Gleichstand kleinere Id vorn)."""
    return sorted((a for a in accounts if by_league(a)),
                  key=lambda a: (-a.get("TrophyScore", 0), a["AccountId"]))


def period_start(now=None):
    """Beginn der laufenden Wertungsperiode (ClosingPeriod, Live: 1 Tag)."""
    plan = trophies.league_settings()["ScheduleConfigs"][0]
    ref = datetime.datetime.fromisoformat(plan["ReferenceDate"])
    now = now or datetime.datetime.now(datetime.UTC)
    length = _seconds(plan["ClosingPeriod"])
    return _iso(ref + datetime.timedelta(
        seconds=int((now - ref).total_seconds()) // length * length))


def reward_parts(reward):
    """Reward -> ({Wallet-Feld: Betrag}, [Deko-SpecIds])."""
    wallet, decorations = {}, []
    for r in (reward or {}).get("RewardItems") or []:
        a = r.get("CurrencyAmount")
        if a and a["CurrencyType"] in objectives.REWARD_CURRENCY:
            field = objectives.REWARD_CURRENCY[a["CurrencyType"]]
            wallet[field] = wallet.get(field, 0) + a["Amount"]
        elif r.get("ItemType") == "Decoration":
            decorations.append(r["SpecId"])
    return wallet, decorations


def _pick(rewards, rank):
    """Belohnung für Rang `rank` (1-basiert) aus einer Liste je Platz."""
    rewards = rewards or []
    return rewards[rank - 1] if 0 < rank <= len(rewards) else None


def _sub_name(league):
    sub = _sub_league(league)
    names = profiles._texts()
    return (names.get(str((sub.get("Name") or {}).get("OasisName")), ""),
            names.get(str(sub.get("PrefixOasisId")), ""))


def settle(me, players, now=None):
    """Abrechnung für `me` seit seinem letzten Kontakt, oder None.

    Saisonende (seit SEASON_START eine neue Saison): neue Liga aus den
    Kronen, LeagueMaintainReward (gleiche Liga) bzw. LeagueReachedReward
    (höhere), für die Ersten der alten Unterliga LeaderSeasonalRewards
    (Deko) und LeagueRankReached-News, dazu LeagueUpdated-News.
    Periodenende (seit PERIOD_START ein neuer Tag): für die Ersten der
    Unterliga LeaderClosingPeriodRewards (Blings), für die Weltersten
    WorldLeaderPeriodRewards (Gold) mit WorldRankReached-News; abgerechnet
    wird einmal mit dem aktuellen Stand, verpasste Tage zählen nicht.

    Ergebnis: {Marks (Account-Felder), League (neue Liga oder None),
    Previous (Saisonergebnis oder None), Rewards [Reward], News [NewsData]}.
    """
    season, period = season_start(now), period_start(now)
    new_season = me.get(SEASON_START, season) < season
    new_period = me.get(PERIOD_START, period) < period
    if not (new_season or new_period):
        return None
    everyone = _everyone(me, players)
    league = trophies.account_league(me)
    ids = lambda xs: [a["AccountId"] for a in xs]
    league_rank = ids(_ranked(everyone, lambda a: trophies.account_league(a) == league)
                      ).index(me["AccountId"]) + 1
    world_rank = ids(_ranked(everyone, lambda a: True)).index(me["AccountId"]) + 1
    sub = _sub_league(league)
    out = {"Marks": {SEASON_START: season, PERIOD_START: period},
           "League": None, "Previous": None, "Rewards": [], "News": []}
    if new_period:
        out["Rewards"].append(_pick(sub.get("LeaderClosingPeriodRewards"), league_rank))
        world = _pick(trophies.league_settings().get("WorldLeaderPeriodRewards"), world_rank)
        if world:
            out["Rewards"].append(world)
            out["News"].append({"$type": T("WorldRankReachedNewsData"),
                                "Type": NEWS_WORLD_RANK_REACHED,
                                "RequiresUserAction": False, "WorldRank": world_rank})
    if new_season:
        old, new = league, trophies.league_of(me.get("TrophyScore", 0))
        out["League"] = new
        out["Previous"] = {"LeagueId": old[0], "SubLeagueId": old[1],
                           "LeagueRank": league_rank, "WorldRank": world_rank,
                           "Viewed": False}
        out["Rewards"].append(_sub_league(new).get("LeagueMaintainReward") if new == old
                              else _sub_league(new).get("LeagueReachedReward")
                              if new > old else None)
        name, prefix = _sub_name(new)
        leader = _pick(sub.get("LeaderSeasonalRewards"), league_rank)
        if leader:
            out["Rewards"].append(leader)
            out["News"].append({
                "$type": T("LeagueRankReachedNewsData"), "Type": NEWS_LEAGUE_RANK_REACHED,
                "RequiresUserAction": False, "LeagueId": old[0], "SubLeagueId": old[1],
                "LeagueRank": league_rank, "IsPromoted": new > old,
                "IsMaintained": new == old, "SubLeagueName": name,
                "SubLeaguePrefixName": prefix,
                "SubLeagueSmallIconUrl": _sub_league(new).get("SmallIconUrl", "")})
        out["News"].append({
            "$type": T("LeagueUpdatedNewsData"), "Type": NEWS_LEAGUE_UPDATED,
            "RequiresUserAction": False,
            "CurrentLeagueId": new[0], "CurrentSubleagueId": new[1],
            "PreviousLeagueId": old[0], "PreviousSubleagueId": old[1],
            "IsUnranked": new[0] == 1, "IsPlaced": old[0] == 1 and new[0] > 1,
            "IsPromoted": new > old and old[0] != 1, "IsDemoted": new < old and new[0] != 1,
            "IsMaintained": new == old and new[0] != 1,
            "SubLeagueName": name, "SubLeaguePrefixName": prefix})
    out["Rewards"] = [balance.scale_reward(r, "Leagues") for r in out["Rewards"] if r]
    return out


def _everyone(me, players):
    others = [a for a in players if a["AccountId"] != me["AccountId"]]
    return [me] + others + list(_fake_accounts())


@functools.cache
def _fake_accounts():
    """Ersatzspieler als Mini-Accounts (wie profiles.fake_account, ohne Burg)."""
    return tuple({"AccountId": c["AccountId"], "DisplayName": c["AccountDisplayName"],
                  "CountryCode": c.get("CountryCode", ""),
                  "TrophyScore": trophies.castle_score(c),
                  "BuildInfo": {"Level": c.get("Level", 1)}}
                 for c in castles.fake_player_castles())


def _entry(a, header):
    return {"$type": T("LeaderboardEntry"),
            "AccountSummary": profiles.account_summary(a, header(a["AccountId"])),
            "IsCastleAttackable": True, "Score": a.get("TrophyScore", 0),
            "Seconds": 0}


def page(me, players, query, header, now=None):
    """LeaderboardPage für den Spieler `me` (Account-Snapshot); `players`
    sind alle echten Accounts, `header` liefert den Clan-Kopf einer Id."""
    def q(k, d: Any = 0):
        return query.get(k, [d])[0]

    num = lambda k: int(q(k)) if str(q(k)).lstrip("-").isdigit() else 0
    level, seek, size = num("level") or LEAGUES, num("seekMode") or MY_RANK, num("pageSize") or 10
    everyone = _ranked(_everyone(me, players), lambda a: True)
    if level == LEAGUES:
        league = (num("leagueId"), num("subLeagueId"))
        if league[0] == 0:
            league = trophies.account_league(me)
        board = [a for a in everyone if trophies.account_league(a) == league]
    elif level == COUNTRY:
        code = q("geoInfo", "") or me.get("CountryCode", "")
        board = [a for a in everyone if a.get("CountryCode", "") == code]
    elif level == FRIENDS:
        ids = {f["Id"] for f in me.get("Friends") or []} | {me["AccountId"]}
        board = [a for a in everyone if a["AccountId"] in ids]
    else:  # Welt; Zonen kennt der Server nicht
        board = everyone
    ids = [a["AccountId"] for a in board]
    if seek == LAST:
        start = max(0, len(board) - size)
    elif seek in (PREVIOUS, NEXT) and num("refAccountId") in ids:
        ref = ids.index(num("refAccountId"))
        start = max(0, ref - size) if seek == PREVIOUS else ref + 1
    elif seek == MY_RANK and me["AccountId"] in ids:
        start = ids.index(me["AccountId"]) // size * size
    else:
        start = 0
    end = season_end(now)
    now = now or datetime.datetime.now(datetime.UTC)
    return {
        "$type": T("LeaderboardPage"),
        "ActiveCountries": (sorted({a.get("CountryCode") for a in everyone if a.get("CountryCode")})
                            if q("returnGeoFilters", "") == "true" else []),
        "ActiveZones": [],
        "CurrentUser": _entry(me, header),
        "Entries": [_entry(a, header) for a in board[start:start + size]],
        "FirstEntryRank": start + 1,
        "Leaders": [_entry(a, header) for a in board[:trophies.league_settings()["LeaderDisplayedCount"]]],
        "NextSeasonStartingDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Ergebnis der letzten Saison: der Client zeigt dazu das
        # Saisonende-Fenster ("Status Quo!", "Promoted!" ...); nur bis es
        # gesehen ist (Original: LastLeagueRewardViewedDate)
        "PreviousLeagueInfo": previous_league_info(me),
        "RemainingTime": f"{max(0, int((end - now).total_seconds()))}s",
        "TotalCount": len(board),
        "WorldLeader": _entry(everyone[0], header),
    }


def previous_league_info(me):
    """PreviousLeagueInfo der letzten Saison, solange noch nicht gezeigt."""
    prev = me.get(PREVIOUS_SEASON)
    if not prev or prev.get("Viewed"):
        return None
    return {"$type": T("PreviousLeagueInfo"),
            "PreviousLeagueId": prev["LeagueId"], "PreviousSubLeagueId": prev["SubLeagueId"],
            "PreviousLeagueRank": prev["LeagueRank"], "PreviousWorldRank": prev["WorldRank"]}