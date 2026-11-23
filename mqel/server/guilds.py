"""Clans (GuildService): Clans aller Spieler.

Ablage: {"Guilds": {Id: {..., "Members": [{AccountId, Title, JoinDate}]}}}.
Mitgliederdaten (Name, Level, ...) kommen beim Ausliefern aus dem
Spielstand des Mitglieds (account_lookup).

Keine Original-Mitschnitte; Form aus den JSON-Lesern des Clients
(tools/client_members.py): Guild, GuildMember, GuildHeader, GuildSummary,
GuildLeaderboardPage/-Entry, ProfanityFilteredString. Der Client liest das
Ergebnis von CreateGuild polymorph über "$type"; ohne Objekt
(leere Antwort) beendet er sich.

GuildMemberTitle als Name (Leser 0x53b690: Unknown 0, Member 1, Leader 10).
"""

import json
import os
import re
import threading

import castles
import profiles
from profanity import filter_text

T = castles.T
MAXIMUM_BATTLE_LOG = 100  # BattleLogSettings.MaximumDefendLog
# RecruitmentMode laut Enumleser des Clients (0x53b720): JoinRequest 1,
# ImmediateJoin 2, Closed 3 -- der Client schickt die Zahl, wir geben sie
# unverändert zurück


# Sprachen der Clan-Texte (GuildSettings.Languages); der Client sucht den
# gefilterten Text seiner Sprache in der Liste
LANGUAGES = ["EN", "FR", "DE", "IT", "ES", "PT", "RU", "PL", "HU"]


def _text(value):
    """ProfanityProofDescription (Client-Leser 0x5e6630): Raw + Filtered
    [ProfanityFilteredDescription {Language, Value}]. Ein flacher
    ProfanityFilteredString ließ den Client im Clan-Fenster abstürzen
    (GuildController sucht in Filtered nach der Sprache)."""
    value = value or ""
    filtered = filter_text(value)
    return {"$type": T("ProfanityProofDescription"), "Raw": value,
            "Filtered": [{"$type": T("ProfanityFilteredDescription"),
                          "Language": code, "Value": filtered}
                         for lang in LANGUAGES for code in (lang, lang.lower())]}


class Guilds:
    def __init__(self, path, account_lookup, log=print):
        """account_lookup(AccountId) -> Account-Dict (oder None)."""
        self.path, self.lookup, self.log = path, account_lookup, log
        self.lock = threading.Lock()
        self.guilds = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("Guild"):  # Ein-Spieler-Format
                g = data["Guild"]
                g["Members"] = [{"AccountId": g.pop("Leader"), "Title": "Leader",
                                 "JoinDate": g.pop("JoinDate")}]
                self.guilds = {g["Id"]: g}
            else:
                self.guilds = {int(k): v for k, v in (data.get("Guilds") or {}).items()}

    def _save(self):
        if self.path:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"Guilds": self.guilds}, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)

    def guild_of(self, account_id):
        return next((g for g in self.guilds.values()
                     if any(m["AccountId"] == account_id for m in g["Members"])),
                    None)

    def name_error(self, name, own_id=None):
        """Fehler-Typ für einen Clan-Namen oder None: Regel wie bei
        Spielernamen (AccountManager: 3-16 druckbare ASCII-Zeichen ohne
        "@"; die Clan-Regel des Originals ist nicht überliefert, längere
        Namen schneidet der Client ab, Leerzeichen sind erlaubt), sonst
        ohne Groß-/Kleinschreibung vergeben."""
        if not re.fullmatch(r"[\x20-\x3f\x41-\x7e]{3,16}", name or "") or name != name.strip():
            return "Exceptions.InvalidGuildDisplayNameException"
        if any(g["DisplayName"].casefold() == name.casefold()
               for g in self.guilds.values() if g["Id"] != own_id):
            return "Exceptions.AccountCreation.GuildDisplayNameAlreadyExistsException"
        return None

    def create(self, account, body, now):
        """Neuer Clan mit dem Gründer als Anführer. Ist er schon in einem
        Clan (Original: AlreadyMemberOfAGuildException), kommt dessen Clan
        zurück: ohne Clan-Objekt beendet sich der Client."""
        with self.lock:
            own = self.guild_of(account["AccountId"])
            if own:
                self.log("    [guild] schon Mitglied eines Clans")
                return self.guild_info(own["Id"])
            gid = max(self.guilds, default=0) + 1
            self.guilds[gid] = {
                "Id": gid,
                "DisplayName": body.get("displayName", ""),
                "Description": body.get("description", ""),
                "WelcomeMessage": body.get("welcomeMessage", ""),
                "LanguageCode": body.get("languageCode") or "en",
                "EmblemId": body.get("emblemId", 1),
                "RecruitmentMode": body.get("recruitmentMode", 1),
                "Members": [{"AccountId": account["AccountId"], "Title": "Leader",
                             "JoinDate": now}],
            }
            self._save()
            self.log(f"    [guild] Clan {gid} \"{body.get('displayName', '')}\" gegründet")
        return self.guild_info(gid)

    def update(self, account, body):
        with self.lock:
            g = self.guild_of(account["AccountId"])
            if not g:
                return None
            for src, dst in (("displayName", "DisplayName"),
                             ("description", "Description"),
                             ("welcomeMessage", "WelcomeMessage"),
                             ("languageCode", "LanguageCode"),
                             ("emblemId", "EmblemId"),
                             ("recruitmentMode", "RecruitmentMode")):
                if src in body:
                    g[dst] = body[src]
            self._save()
            self.log(f"    [guild] Clan {g['Id']} geändert")
        return self.guild_info(g["Id"])

    def add_member(self, leader_id, account_id, now):
        """Spieler in den Clan von `leader_id` aufnehmen (Dummy-Spieler
        nehmen Einladungen sofort an). False, wenn es keinen Clan gibt oder
        der Spieler schon in einem ist."""
        with self.lock:
            g = self.guild_of(leader_id)
            if not g or self.guild_of(account_id):
                return False
            g["Members"].append({"AccountId": account_id, "Title": "Member",
                                 "JoinDate": now})
            self._save()
        self.log(f"    [guild] {account_id} tritt Clan {g['Id']} bei")
        return True

    def join(self, guild_id, account_id, now):
        """Beitritt per SendJoinRequest. Nur Clans mit ImmediateJoin (2)
        nehmen sofort auf; Beitrittsanfragen (JoinRequest 1) fehlen noch."""
        with self.lock:
            g = self.guilds.get(guild_id)
            if not g or self.guild_of(account_id):
                return False
            if g.get("RecruitmentMode") != 2:
                self.log(f"    [guild] Clan {guild_id}: Beitrittsanfragen fehlen noch")
                return False
            g["Members"].append({"AccountId": account_id, "Title": "Member",
                                 "JoinDate": now})
            self._save()
        self.log(f"    [guild] {account_id} tritt Clan {guild_id} bei")
        return True

    def remove_member(self, account_id, member_id):
        """Austreten (member_id == account_id) oder Rauswerfen durch den
        Anführer. Geht der Anführer, übernimmt das dienstälteste Mitglied;
        ein leerer Clan wird gelöscht. Gibt (GuildId, Clan besteht noch)
        zurück oder None."""
        with self.lock:
            g = self.guild_of(member_id)
            if not g:
                return None
            if member_id != account_id and not any(
                    m["AccountId"] == account_id and m["Title"] == "Leader"
                    for m in g["Members"]):
                return None
            g["Members"] = [m for m in g["Members"] if m["AccountId"] != member_id]
            if not g["Members"]:
                del self.guilds[g["Id"]]
            elif not any(m["Title"] == "Leader" for m in g["Members"]):
                min(g["Members"], key=lambda m: m["JoinDate"])["Title"] = "Leader"
            self._save()
        self.log(f"    [guild] {member_id} verlässt Clan {g['Id']}")
        return g["Id"], bool(g["Members"])

    def set_title(self, account_id, member_id, title):
        """Titel ändern (nur der Anführer). Ein neuer Anführer löst den
        bisherigen ab, der Mitglied wird. Gibt [(AccountId, Titel)] der
        geänderten Mitglieder und die GuildId zurück oder None."""
        name = {1: "Member", 10: "Leader"}.get(title)
        with self.lock:
            g = self.guild_of(account_id)
            members = {m["AccountId"]: m for m in g["Members"]} if g else {}
            if (g is None or not name or member_id not in members
                    or members.get(account_id, {}).get("Title") != "Leader"):
                return None
            changed = [(member_id, name)]
            if name == "Leader" and member_id != account_id:
                changed.append((account_id, "Member"))
            for aid, t in changed:
                members[aid]["Title"] = t
            self._save()
        self.log(f"    [guild] Clan {g['Id']}: {changed}")
        return g["Id"], changed

    @staticmethod
    def _emblem(g):
        return {"$type": T("GuildEmblem"), "Id": g["EmblemId"]}

    def header(self, account_id):
        """GuildHeader des Clans von `account_id` (oder None)."""
        g = self.guild_of(account_id)
        if not g:
            return None
        return {"$type": T("GuildHeader"), "Id": g["Id"],
                "DisplayName": g["DisplayName"], "Emblem": self._emblem(g)}

    def _accounts(self, g):
        return [(m, self.lookup(m["AccountId"])) for m in g["Members"]]

    @staticmethod
    def _trophies(members):
        return sum((a or {}).get("TrophyScore", 0) for _, a in members)

    def summary(self, g):
        members = self._accounts(g)
        leader = next((a for m, a in members if m["Title"] == "Leader"), None) or {}
        return {"$type": T("GuildSummary"), "Id": g["Id"],
                "DisplayName": g["DisplayName"], "Emblem": self._emblem(g),
                "LanguageCode": g["LanguageCode"].upper(),
                "LeaderAccountId": leader.get("AccountId", 0),
                "LeaderDisplayName": leader.get("DisplayName", ""),
                "MemberCount": len(members), "TrophyScore": self._trophies(members)}

    def guild_info(self, guild_id):
        """Guild (vollständig) oder None, wenn es den Clan nicht gibt."""
        g = self.guilds.get(guild_id)
        if not g:
            return None
        members = [(m, a) for m, a in self._accounts(g) if a]
        header = {"$type": T("GuildHeader"), "Id": g["Id"],
                  "DisplayName": g["DisplayName"], "Emblem": self._emblem(g)}
        # Sprachcodes wie in GuildSettings.Languages ("EN", ...); der Client
        # schickt "en", stürzte mit "en" in der Antwort aber im Clan-Fenster ab
        return {
            "$type": T("Guild"), "Id": g["Id"], "DisplayName": g["DisplayName"],
            "Description": _text(g["Description"]),
            "WelcomeMessage": _text(g["WelcomeMessage"]),
            "LanguageCode": g["LanguageCode"].upper(), "Emblem": self._emblem(g),
            "RecruitmentMode": g["RecruitmentMode"],
            "Members": [{"$type": T("GuildMember"),
                         "AccountSummary": profiles.account_summary(a, header),
                         "JoinDate": m["JoinDate"], "Title": m["Title"]}
                        for m, a in members],
            "InviteRequests": [], "JoinRequests": [],
            "PendingValidationCastleIds": [], "Rank": self._rank(g["Id"]),
            "TrophyScore": self._trophies(members),
            "Stats": {"$type": T("GuildStats")},  # Typ verlangt (Leser prüft $type)
        }

    def record_battle(self, entry):
        """BattleLogEntry eines Spielerangriffs in die Kampflogs der Clans
        von Angreifer und Verteidiger (neueste vorn, höchstens
        BattleLogSettings.MaximumDefendLog wie das Verteidigungslog)."""
        ids = {entry["AttackerAccountSummary"]["Id"], entry["DefenderAccountSummary"]["Id"]}
        with self.lock:
            guilds = {g["Id"]: g for g in map(self.guild_of, ids) if g}
            for g in guilds.values():
                g["BattleLog"] = [entry] + (g.get("BattleLog") or [])[:MAXIMUM_BATTLE_LOG - 1]
            if guilds:
                self._save()

    def battle_log(self, guild_id):
        """GuildBattleLog (GuildService.GetBattleLog); unbekannter Clan: leer."""
        return {"$type": T("GuildBattleLog"),
                "BattleLogEntries": (self.guilds.get(guild_id) or {}).get("BattleLog") or []}

    def account_guild(self, account_id):
        """Guild für Account.Guild (GetAccountInformation) oder None."""
        g = self.guild_of(account_id)
        return self.guild_info(g["Id"]) if g else None

    def _ranked(self):
        return sorted(self.guilds.values(),
                      key=lambda g: (-self._trophies(self._accounts(g)), g["Id"]))

    def _rank(self, guild_id):
        return 1 + [g["Id"] for g in self._ranked()].index(guild_id)

    def leaderboard(self, account):
        entries = [{"$type": T("GuildLeaderboardEntry"), "GuildSummary": self.summary(g),
                    "Score": self._trophies(self._accounts(g)), "Seconds": 0}
                   for g in self._ranked()]
        own = self.guild_of(account["AccountId"])
        current = next((e for e in entries
                        if own and e["GuildSummary"]["Id"] == own["Id"]), None)
        return {"$type": T("GuildLeaderboardPage"), "Entries": entries,
                "Leaders": [], "FirstEntryRank": 1, "TotalCount": len(entries),
                "CurrentGuild": current, "RemainingTime": "1d"}

    def search(self):
        return {"$type": T("GuildSearchResult"), "HasMore": False,
                "GuildSummaries": [self.summary(g) for g in self._ranked()]}