"""AdminService.hqs für die Original-GameplayToolbox (toolbox/).

Der Dienst ist im Original [Private]: nur mit HMAC-Signatur (Util.Common.
Service.WebRequestHelper). Schlüssel-Id und Secret stehen in
server/state/admin_hmac.json (beim ersten Start zufällig erzeugt) und
gehören in die app.config.json der Toolbox (HMACAccessKeyID, HMACSecret).
Ohne gültige Signatur antwortet der Server wie das Original mit 404.

Antworten sind das nackte Ergebnis-JSON (BodyStyle bare, wie die Toolbox
es liest), Fehler eine ServiceException mit Content-Type genau
"application/json" (sonst wertet Util.Client sie nicht aus).

Bisher nur Lesemethoden; die Toolbox ruft beim Start
SearchAccount(Email aus ihren Einstellungen) und GetAccountSummary.
"""

import base64
import datetime
import email.utils
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from typing import ClassVar

import account_state
import anticheat
import auth
import balance
import castles
import chat_log
import loot
import notifications
import profiles
import server_stats
import special_packs
import tracking
import trophies
import website

# Zeitfenster der Signatur (Date-Header), gegen Wiederholen mitgeschnittener Aufrufe
MAX_CLOCK_SKEW = 300

# Spieler-Mail = Token + Suffix (Login-Seite des Launchers, patcher.py)
EMAIL_SUFFIX = "@mqel.local"


class AdminError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def load_keys(path):
    """{AccessKeyId: Secret} aus path; legt die Datei mit einem zufälligen
    Schlüssel an, falls sie fehlt."""
    if not os.path.exists(path):
        keys = {"AccessKeyId": "toolbox", "Secret": secrets.token_urlsafe(32)}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(keys, f, indent=1)
        print(f"[admin] neuer HMAC-Schlüssel in {path}", flush=True)
    with open(path, encoding="utf-8") as f:
        keys = json.load(f)
    return {keys["AccessKeyId"]: keys["Secret"]}


def _dotnet_url_encode(text):
    """HttpUtility.UrlEncode (.NET 4): Leerzeichen als +, sicher sind
    Buchstaben, Ziffern und -_.!*(), Escapes in Kleinbuchstaben."""
    out = []
    for b in text.encode("utf-8"):
        c = chr(b)
        if c.isascii() and (c.isalnum() or c in "-_.!*()"):
            out.append(c)
        elif c == " ":
            out.append("+")
        else:
            out.append(f"%{b:02x}")
    return "".join(out)


def signature(path_and_query, method, date, access_key, secret):
    """WebRequestHelper.GenerateHMACSignature."""
    text = "\n".join([_dotnet_url_encode(path_and_query.lower()), method,
                      date, access_key, secret])
    digest = hmac.new(secret.encode("utf-8"), text.encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def authorized(keys, path_and_query, method, headers, now=None):
    given = headers.get("content-hmac") or ""
    key_id = headers.get("content-accesskeyid") or ""
    date = headers.get("Date") or ""
    secret = keys.get(key_id)
    if not (given and secret and date):
        return False
    try:
        sent = email.utils.parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=datetime.UTC)
    now = now or datetime.datetime.now(datetime.UTC)
    if abs((now - sent).total_seconds()) > MAX_CLOCK_SKEW:
        return False
    expected = signature(path_and_query, method, date, key_id, secret)
    return hmac.compare_digest(given, expected)


# InventoryItem.Type der Toolbox (InventoryItemType, Vorgabe 1): sie unterscheidet
# Ausrüstung und Tränke daran, unsere Spielstände führen das Feld nicht
ITEM_TYPES = {castles.T("HeroEquipmentItem"): 1, castles.T("HeroConsumableItem"): 2}


def _with_item_types(node):
    """Kopie von node mit Type an jedem Inventar-Item."""
    if isinstance(node, list):
        return [_with_item_types(v) for v in node]
    if isinstance(node, dict):
        out = {k: _with_item_types(v) for k, v in node.items()}
        if out.get("$type") in ITEM_TYPES:
            out["Type"] = ITEM_TYPES[out["$type"]]
        return out
    return node


def _consumable_name(template, template_id):
    """Anzeigename eines Verbrauchsguts: Art aus dem Template-Typ (ohne
    Namen in den Settings), z. B. "HealthPotion 1", "CraftingMaterial 1002"."""
    kind = template.get("$type", "?").split(",")[0].split(".")[-1]
    return f"{kind.replace('ConsumableTemplate', '')} {template_id}"


def _email(token):
    return token + EMAIL_SUFFIX


HERO_NAMES = {2: "Knight", 3: "Archer", 4: "Mage", 5: "Runaway"}
WALLET_FIELDS = ("InGameCoin", "LifeForce", "PremiumCash")


def _ledger(path):
    """Einträge eines Kontobuchs (fehlt: keine)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class AdminService:
    def __init__(self, players, guilds, maintenance_path=None, chat=None, news=None):
        self.players, self.guilds = players, guilds
        self.news = news  # news.News (Launcher-Seite)
        self.chat = chat  # chat_log.ChatLog des Profils
        self.maintenance_path = maintenance_path
        self.maintenance = False
        if maintenance_path and os.path.exists(maintenance_path):
            with open(maintenance_path, encoding="utf-8") as f:
                self.maintenance = bool(json.load(f).get("Maintenance"))
        self.data = account_state.GameData()  # Kataloge (Templates, Verbrauchsgüter)

    # --- Hilfen -------------------------------------------------------------

    def _account(self, account_id):
        p = self.players.by_account_id(int(account_id))
        if p is None:
            raise AdminError(404, f"Unknown account {account_id}")
        return p, p.state.snapshot()

    def _news(self):
        """Die News-Verwaltung; ohne sie (AdminService ohne news=) 404 statt AttributeError."""
        if self.news is None:
            raise AdminError(404, "News are not available")
        return self.news

    def _token(self, account_id):
        return next((t for t, a in self.players.index.items() if a == account_id), "")

    def _search_entry(self, a):
        return {"$type": castles.T("AccountSummaryForSearch"),
                "Id": a["AccountId"], "DisplayName": a.get("DisplayName") or "",
                "Email": _email(self._token(a["AccountId"])),
                "Privileges": a.get("Privileges", 0),
                "TrophyScore": a.get("TrophyScore", 0),
                "CountryCode": a.get("CountryCode") or "",
                "IsCastleAttackable": a.get("IsCastleAttackable", True),
                "SpecialPacks": a.get("SpecialPacks") or [],
                "AvatarId": a.get("AvatarId", 0)}

    # --- Methoden (Name = IAdminService) -----------------------------------

    def HealthCheck(self, args):
        return None

    def SearchAccount(self, args):
        """ByDisplayName (0): Teil des Anzeigenamens; ByEmail (1): Mail
        bzw. Token genau. Nur echte Spieler, keine Ersatzspieler."""
        query = str(args.get("query") or "").strip()
        n = int(args.get("maxResult") or 100)
        by_email = int(args.get("accountSearchType") or 0) == 1
        q = query.casefold()
        found = []
        for token, account_id in sorted(self.players.index.items()):
            a = self.players.by_account_id(account_id).state.snapshot()
            if by_email:
                hit = q in (token.casefold(), _email(token).casefold())
            else:
                hit = bool(q) and q in (a.get("DisplayName") or "").casefold()
            if hit:
                found.append(a)
        found.sort(key=lambda a: (a.get("DisplayName") or "").casefold())
        return {"$type": castles.T("ProfileSearchResult"), "MaxResult": n,
                "Query": query,
                "Results": [self._search_entry(a) for a in found[:n]]}

    def GetAccountSummary(self, args):
        p, a = self._account(args["accountId"])
        league, sub = trophies.account_league(a)
        return {"$type": castles.T("AccountSummaryForTools"),
                "Id": a["AccountId"], "SpecialPacks": a.get("SpecialPacks") or [],
                "CastleThemeId": (a.get("BuildInfo") or {}).get("ThemeId", 0),
                "DisplayName": a.get("DisplayName") or "",
                "Email": _email(p.name), "Privileges": a.get("Privileges", 0),
                "SelectedHeroId": a.get("SelectedHeroId"),
                "TrophyScore": a.get("TrophyScore", 0),
                "CountryCode": a.get("CountryCode") or "",
                "ProfanityFiltering": a.get("ProfanityFiltering", False),
                "LeagueId": league, "SubLeagueId": sub,
                "IsCastleAttackable": a.get("IsCastleAttackable", True)}

    def GetAccountHeroes(self, args):
        return self._account(args["accountId"])[1].get("Heroes") or []

    def GetAccountHero(self, args):
        hero_id = int(args["heroSpecContainerId"])
        heroes = self.GetAccountHeroes(args)
        return next((h for h in heroes if h["HeroSpecContainerId"] == hero_id), None)

    def GetAccountInventory(self, args):
        return self._account(args["accountId"])[1].get("Inventory")

    def GetWallet(self, args):
        return self._account(args["accountId"])[1].get("Wallet")

    def GetWalletCurrencies(self, args):
        w = self.GetWallet(args) or {}
        return [{"$type": castles.T("CurrencyAmount"), "CurrencyType": t, "Amount": w.get(f, 0)}
                for t, f in ((1, "PremiumCash"), (2, "InGameCoin"), (4, "LifeForce"))]

    def GetBuildInfo(self, args):
        return self._account(args["accountId"])[1].get("BuildInfo")

    def GetShieldInfo(self, args):
        due = castles.shield_due(self._account(args["currentAccountId"])[1])
        return {"$type": castles.T("ShieldInfo"), "IsShielded": due is not None,
                "ShieldExpirableDueDate": due or "0001-01-01T00:00:00Z"}

    def GetFriends(self, args):
        return self._account(args["accountId"])[1].get("Friends") or []

    def GetAccountFriendInvitations(self, args):
        return self._account(args["accountId"])[1].get("FriendshipInvitations") or []

    def GetAccountCompletedAssignments(self, args):
        return self._account(args["accountId"])[1].get("CompletedAssignments") or []

    def GetAccountCompletedAchievements(self, args):
        return self._account(args["accountId"])[1].get("CompletedAchievements") or []

    def GetAccountObjective(self, args):
        return self._account(args["accountId"])[1].get("Objectives") or []

    def GetAccountAttackRegion(self, args):
        return self._account(args["accountId"])[1].get("AttackRegions") or []

    def GetUnlockedSpells(self, args):
        return self._account(args["accountId"])[1].get("UnlockedSpells") or []

    def GetAccountChallenges(self, args):
        return self._account(args["accountId"])[1].get("Challenges") or []

    # Wettbewerbs-Challenges (Custom Challenges von 2014) gibt es bei uns nicht
    def GetCompetitionChallengeSummaries(self, args):
        return []

    # Beschleunigung der Bauzeiten gibt es bei uns nicht: immer 1
    def GetAccelerationFactor(self, args):
        return {"$type": castles.T("AcceleratorFactorInfo"), "AcceleratorFactor": 1.0}

    def GetLatestTrackedData(self, args):
        return self.players.tracker.latest(
            [int(a) for a in args.get("accountIdList") or []],
            [int(t) for t in args.get("trackingTagList") or []],
            int(args.get("maxSize") or 100))

    # --- Webtool (/admin, admin_web.py) -------------------------------------

    def _guild_name(self, account_id):
        g = self.guilds.guild_of(account_id)
        return g.get("DisplayName", "") if g else ""

    def ListAccounts(self, args):
        """Alle echten Spieler für die Übersicht."""
        # alles, was je Account über alle Accounts liefe, einmal vorab
        # (Tracking, Clans); Spielstände ohne Kopie lesen
        out = []
        now = datetime.datetime.now(datetime.UTC)
        cheats = self.players.tracker.latest_by_account(
            [tracking.CHEAT_FAILURE, tracking.CLIENT_HACK])
        guild_names = {m["AccountId"]: g.get("DisplayName", "")
                       for g in list(self.guilds.guilds.values()) for m in g["Members"]}

        def row(a):
            league, sub = trophies.account_league(a)
            return {"DisplayName": a.get("DisplayName") or "",
                    "TrophyScore": a.get("TrophyScore", 0),
                    "League": [league, sub],
                    "CastleLevel": (a.get("BuildInfo") or {}).get("Level", 0),
                    "Heroes": [{"Name": HERO_NAMES.get(h["HeroSpecContainerId"], "?"),
                                "Level": h.get("Level", 1)}
                               for h in a.get("Heroes") or []],
                    "Wallet": {f: (a.get("Wallet") or {}).get(f, 0) for f in WALLET_FIELDS},
                    "CountryCode": a.get("CountryCode") or "",
                    "LastSeenOnline": a.get("LastSeenOnline")}

        for token, account_id in sorted(self.players.index.items()):
            p = self.players.get(token)
            out.append(dict(p.state.read(row), Id=account_id, Login=token,
                            Guild=guild_names.get(account_id, ""),
                            Sanction=p.state.sanction(now),
                            Cheat=self._cheat_from(cheats.get(account_id, []))))
        out.sort(key=lambda a: a["LastSeenOnline"] or "", reverse=True)  # zuletzt online zuerst
        return out

    def ClearCheatFlags(self, args):
        """Cheat-Verdacht eines Spielers zurücksetzen (Fehlalarme): löscht
        seine CheatFailure- und ClientHack-Einträge aus dem Tracking."""
        p, _ = self._account(args["accountId"])
        return {"Removed": self.players.tracker.remove(
            p.account_id, {tracking.CHEAT_FAILURE, tracking.CLIENT_HACK})}

    def DeleteAccount(self, args):
        """Account ohne Helden löschen (z. B. im Tutorial hängengeblieben)."""
        p, _ = self._account(args["accountId"])
        try:
            self.players.delete(p.account_id)
        except ValueError:
            raise AdminError(400, "Only accounts without heroes can be deleted")
        return {}

    def _cheat_summary(self, account_id):
        """Verdacht aus dem Tracking (anticheat.py): Anzahl CheatFailure und
        Namen der zuletzt gemeldeten ClientHackFlags."""
        return self._cheat_from([json.loads(e) for e in self.players.tracker.latest(
            [account_id], [tracking.CHEAT_FAILURE, tracking.CLIENT_HACK], tracking.KEEP)])

    @staticmethod
    def _cheat_from(events):
        """Zusammenfassung aus den Ereignissen eines Accounts, neueste zuerst."""
        hack = next((e for e in events if e["TrackingTagId"] == tracking.CLIENT_HACK), {})
        return {"Failures": sum(e["TrackingTagId"] == tracking.CHEAT_FAILURE for e in events),
                "Flags": [k for k in anticheat.HACK_FLAGS.values() if hack.get(k)]}

    def GetAccountDetail(self, args):
        """Spielstand eines Spielers, aufbereitet für die Detailseite."""
        p, a = self._account(args["accountId"])
        items = [dict(self._item(it["Item"]), Slot=it["SlotIndex"],
                      Count=it.get("StackCount", 1))
                 for it in sorted((a.get("Inventory") or {}).get("HeroItems") or [],
                                  key=lambda s: s["SlotIndex"])]
        league, sub = trophies.account_league(a)
        due = castles.shield_due(a)
        packs = {x["Id"]: x.get("DebugName", "") for x in special_packs._settings()["SpecialPacks"]}
        return {
            "Id": a["AccountId"], "Login": p.name,
            "HasPassword": self.players.auth.has_hash(p.name),
            "SteamId": self.players.auth.steam_id(p.name),
            "IsAdmin": self.players.auth.is_admin(p.name), "DisplayName": a.get("DisplayName") or "",
            "CountryCode": a.get("CountryCode") or "", "TrophyScore": a.get("TrophyScore", 0),
            "League": [league, sub], "LastSeenOnline": a.get("LastSeenOnline"),
            "CastleLevel": (a.get("BuildInfo") or {}).get("Level", 0),
            "Guild": self._guild_name(a["AccountId"]),
            "GuildId": (self.guilds.guild_of(a["AccountId"]) or {}).get("Id"),
            "Wallet": a.get("Wallet") or {}, "Shield": due,
            "Sanction": p.state.sanction(datetime.datetime.now(datetime.UTC)),
            "Heroes": [{"Id": h["HeroSpecContainerId"],
                        "Name": HERO_NAMES.get(h["HeroSpecContainerId"], "?"),
                        "Level": h.get("Level", 1), "XP": h.get("XP", 0),
                        "Selected": h["HeroSpecContainerId"] == a.get("SelectedHeroId"),
                        "Equipment": [dict(self._item(it), Slot=slot)
                                      for slot, it in (h.get("Equipment") or {}).items()
                                      if isinstance(it, dict)],
                        "Belt": [{"Slot": b["SlotIndex"], "Count": b.get("StackCount", 0),
                                  "Name": _consumable_name(
                                      self.data.consumables.get(b.get("TemplateId"), {}),
                                      b.get("TemplateId"))}
                                 for b in h.get("EquippedConsumables") or []
                                 if b.get("TemplateId")]}
                       for h in a.get("Heroes") or []],
            "Inventory": items,
            "InventorySlots": (a.get("Inventory") or {}).get("InventoryTabCount", 0)
            * account_state.INVENTORY_SLOTS_PER_TAB,
            "Objectives": len(a.get("Objectives") or []),
            "Achievements": len(a.get("CompletedAchievements") or []),
            "Friends": [{"Id": f.get("Id"), "DisplayName": f.get("DisplayName", "")}
                        for f in a.get("Friends") or []],
            "SpecialPacks": [{"Id": i, "Name": packs.get(i, "")}
                             for i in a.get("SpecialPacks") or []],
            "Cheat": self._cheat_summary(p.account_id),
        }

    def _item(self, it):
        """Name, Art, Template und Stufe eines Items für die Anzeige."""
        tid = it.get("TemplateId")
        if it.get("$type") == castles.T("HeroEquipmentItem"):
            return {"Kind": "Equipment", "TemplateId": tid, "Level": it.get("ItemLevel"),
                    "Name": self.data.templates.get(tid, {}).get("InternalDescription", "?")}
        return {"Kind": "Consumable", "TemplateId": tid, "Level": None,
                "Name": _consumable_name(self.data.consumables.get(tid, {}), tid)}

    def _member_name(self, account_id):
        p = self.players.by_account_id(account_id)
        a = p.state.snapshot() if p else profiles.fake_account(account_id)
        return (a or {}).get("DisplayName") or str(account_id), p is not None

    def ListGuilds(self, args):
        out = []
        for g in sorted(self.guilds.guilds.values(), key=lambda g: self.guilds._rank(g["Id"])):
            members = self.guilds._accounts(g)
            leader = next((m["AccountId"] for m in g["Members"] if m["Title"] == "Leader"), 0)
            out.append({"Id": g["Id"], "DisplayName": g["DisplayName"],
                        "Rank": self.guilds._rank(g["Id"]),
                        "TrophyScore": self.guilds._trophies(members),
                        "Members": len(g["Members"]),
                        "Leader": self._member_name(leader)[0] if leader else "",
                        "LanguageCode": g.get("LanguageCode", "").upper(),
                        "RecruitmentMode": g.get("RecruitmentMode"),
                        "Battles": len(g.get("BattleLog") or [])})
        return out

    def GetGuildDetail(self, args):
        g = self.guilds.guilds.get(int(args["guildId"]))
        if g is None:
            raise AdminError(404, f"Unknown clan {args['guildId']}")
        members = []
        for m, a in self.guilds._accounts(g):
            name, real = self._member_name(m["AccountId"])
            members.append({"Id": m["AccountId"], "DisplayName": name, "IsPlayer": real,
                            "Title": m["Title"], "JoinDate": m["JoinDate"],
                            "TrophyScore": (a or {}).get("TrophyScore", 0)})
        battles = [{"Date": e.get("AttackStartDateTime"),
                    "Attacker": (e.get("AttackerAccountSummary") or {}).get("DisplayName", ""),
                    "Defender": (e.get("DefenderAccountSummary") or {}).get("DisplayName", ""),
                    "Won": e.get("CompletionType") == 0,
                    "DurationMs": e.get("AttackDurationInMilliseconds", 0),
                    "AttackerTrophies": e.get("AttackerTrophyScoreVariation", 0),
                    "DefenderTrophies": e.get("DefenderTrophyScoreVariation", 0)}
                   for e in g.get("BattleLog") or []]
        return {"Id": g["Id"], "DisplayName": g["DisplayName"],
                "Rank": self.guilds._rank(g["Id"]),
                "TrophyScore": self.guilds._trophies(self.guilds._accounts(g)),
                "LanguageCode": g.get("LanguageCode", "").upper(),
                "RecruitmentMode": g.get("RecruitmentMode"),
                "Description": g.get("Description", ""),
                "WelcomeMessage": g.get("WelcomeMessage", ""),
                "Members": members, "Battles": battles}

    # Käufe: Test-Shop (Blings, Pakete), Bling-Shop-Pakete und Spiel-Shop-Käufe
    # mit Blings; Spiel-Shop-Käufe mit Gold/Lebenskraft zählen nicht
    PURCHASE_TAGS: ClassVar[dict[int, str]] = {tracking.PREMIUM_CASH_PURCHASED: "Blings",
                                               tracking.SPECIAL_PACK_PURCHASED: "Pack",
                                               tracking.SHOP: "Shop"}

    def GetPurchases(self, args):
        """IAP (neueste zuerst), optional je Spieler; dazu Summen je Spieler."""
        accounts = [int(args["accountId"])] if args.get("accountId") else []
        packs = {x["Id"]: x.get("DebugName", "") for x in special_packs._settings()["SpecialPacks"]}
        names, rows, totals = {}, [], {}
        for e in map(json.loads, self.players.tracker.latest(
                accounts, list(self.PURCHASE_TAGS), int(args.get("max") or 1000))):
            tag, aid = e["TrackingTagId"], e["AccountId"]
            spent = -(e.get("WalletDelta") or {}).get("PremiumCash", 0)
            if tag == tracking.SHOP and spent <= 0:
                continue
            if aid not in names:
                names[aid] = self._member_name(aid)[0]
            t = totals.setdefault(aid, {"AccountId": aid, "Player": names[aid],
                                        "BlingsBought": 0, "Packs": 0, "BlingsSpent": 0})
            if tag == tracking.SHOP:
                sku = self.data.skus.get(e.get("SkuCode"), {})
                sku_name = sku.get("InternalDescription") or f"SKU {e.get('SkuCode')}"
                item = f"{sku_name} ({int(spent)} Blings)"
                t["BlingsSpent"] += spent
            elif tag == tracking.PREMIUM_CASH_PURCHASED:
                item = f"{e.get('Amount', 0)} Blings"
                t["BlingsBought"] += e.get("Amount", 0)
            else:
                item = packs.get(e.get("SpecialPackId"), f"Pack {e.get('SpecialPackId')}")
                if e.get("Blings"):
                    item += f" ({int(e['Blings'])} Blings)"
                    t["BlingsSpent"] += e["Blings"]
                t["Packs"] += 1
            rows.append({"Date": e["CreationDate"], "AccountId": aid, "Player": names[aid],
                         "Type": self.PURCHASE_TAGS[tag], "Item": item})
        return {"Purchases": rows, "Totals": list(totals.values())}

    def GetCatalog(self, args):
        """Auswahllisten: Ausrüstungs-Templates, Verbrauchsgüter, Pakete, Tracking-Tags."""
        data = self.data
        return {
            "Equipment": [{"Id": t["Id"], "Name": t.get("InternalDescription", ""),
                           "LevelMin": t.get("LevelMin", 1), "LevelMax": t.get("LevelMax", 1)}
                          for t in data.templates.values()],
            "Consumables": [{"Id": c["TemplateId"],
                             "Name": _consumable_name(c, c["TemplateId"]),
                             "MaxStack": c.get("MaxInventoryStackCount", 1)}
                            for c in data.consumables.values()],
            "SpecialPacks": [{"Id": x["Id"], "Name": x.get("DebugName", "")}
                             for x in special_packs._settings()["SpecialPacks"]],
            "TrackingTags": tracking.TAG_NAMES,
        }

    def GetTracking(self, args):
        """Tracking-Ereignisse (neueste zuerst), optional je Spieler/Tags."""
        accounts = [int(args["accountId"])] if args.get("accountId") else []
        tags = [int(t) for t in args.get("tags") or []] or list(tracking.TAG_NAMES)
        return [json.loads(e) for e in self.players.tracker.latest(
            accounts, tags, int(args.get("max") or 200))]

    # Ändernde Aktionen: an den laufenden Client gemeldet und selbst im Tracking

    def UpdateWallet(self, args):
        p, _ = self._account(args["accountId"])
        deltas = {f: int(args.get(f) or 0) for f in WALLET_FIELDS if args.get(f)}
        out = p.state.admin_wallet(deltas)
        if out:
            notifications.wallet_updated(p.notifications, out)
            p.tracker.track(p.account_id, 528,  # EconomicCustomerSupportWalletUpdated
                            {"WalletDelta": out, "Reason": args.get("reason") or ""})
        return out

    def GiveItem(self, args):
        p, _ = self._account(args["accountId"])
        kind, tid = args["kind"], int(args["templateId"])
        n = int(args.get("amount") or 1)
        if kind == "equipment":
            if tid not in p.state.data.templates:
                raise AdminError(400, f"Unknown template {tid}")
            items = [("equipment", tid, n)]
        elif kind == "material":
            if tid not in p.state.data.consumables:
                raise AdminError(400, f"Unknown consumable {tid}")
            items = [("material", tid, n)]
        else:
            raise AdminError(400, "kind must be equipment or material")
        events = p.state.admin_give(items)
        for name, fields in events:
            p.notifications.push(name, fields)
        p.tracker.track(p.account_id, 531,  # EconomicGift
                        {"Kind": kind, "TemplateId": tid, "Amount": n,
                         "Reason": args.get("reason") or ""})
        return {"Events": [name for name, _ in events]}

    def SetHeroLevel(self, args):
        """Held heroId auf level heben (nur aufwärts, XP bis zur Schwelle);
        Meldungen wie nach einem Angriff (HeroXpChanged, neue Zauber)."""
        p, _ = self._account(args["accountId"])
        hero_id, level = int(args["heroId"]), int(args["level"])
        try:
            hero, xp, changed = p.state.admin_hero_level(hero_id, level)
        except KeyError:
            raise AdminError(404, f"Hero {hero_id} not owned")
        except ValueError as e:
            raise AdminError(400, str(e))
        if xp:
            notifications.hero_xp_changed(p.notifications, hero, xp, changed)
        for spell in p.state.take_unlocked_spells():
            notifications.spell_unlocked(p.notifications, spell)
        p.tracker.track(p.account_id, 531,  # EconomicGift
                        {"Kind": "hero_xp", "HeroId": hero_id, "Amount": xp,
                         "Reason": args.get("reason") or ""})
        return {"Level": hero["Level"], "XP": hero["XP"]}

    def SetShield(self, args):
        """hours > 0: Schild für so viele Stunden (ersetzt einen bestehenden);
        0: Schild entfernen."""
        p, _ = self._account(args["accountId"])
        now = datetime.datetime.now(datetime.UTC)
        hours = float(args.get("hours") or 0)
        events = p.state.expire_shield(now, remove=True)
        if hours > 0:
            events += p.state.add_shield(int(hours * 3600), now)
        for name, fields in events:
            p.notifications.push(name, fields)
        return {"Shield": castles.shield_due(p.state.snapshot())}

    def SetSanction(self, args):
        """status "ban" (dauerhaft), "suspend" (hours Stunden) oder "none"
        (aufheben). Wirkt ab der nächsten Anfrage des Clients."""
        p, _ = self._account(args["accountId"])
        status, reason = args["status"], args.get("reason") or ""
        now = datetime.datetime.now(datetime.UTC)
        if status == "ban":
            sanction = {"Status": 2, "Reason": reason, "Since": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        elif status == "suspend":
            hours = float(args.get("hours") or 0)
            if hours <= 0:
                raise AdminError(400, "Suspension needs hours > 0")
            until = now + datetime.timedelta(hours=hours)
            sanction = {"Status": 1, "Reason": reason, "Since": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "Until": until.strftime("%Y-%m-%dT%H:%M:%SZ")}
        elif status == "none":
            sanction = None
        else:
            raise AdminError(400, "status must be ban, suspend or none")
        p.state.set_sanction(sanction)
        if sanction:  # Launcher verlangt neue Anmeldung, die dann abgelehnt wird
            self.players.auth.logout(p.name)
        return {"Sanction": sanction}

    def _chat(self):
        if self.chat is None:
            raise AdminError(404, "No chat log for this profile")
        return self.chat

    def GetChatLog(self, args):
        """Chat-Verlauf (neueste zuerst), optional je Spieler, Raum, Text."""
        login = ""
        if args.get("accountId"):
            login, _ = self._account(args["accountId"])
            login = login.name
        entries = self._chat().search(login or None, args.get("text"),
                                      args.get("room"), int(args.get("max") or 500))
        for e in entries:
            e["AccountId"] = self.players.index.get(e["Login"])
        return {"Entries": entries, "RetentionDays": chat_log.RETENTION_DAYS,
                "Muted": [{"Login": k, "AccountId": self.players.index.get(k), "Until": v}
                          for k, v in sorted(self._chat().muted().items())]}

    def SetChatMute(self, args):
        """Stummschalten (hours, ohne: unbefristet) oder aufheben (mute false).
        Wirkt sofort; der Spieler sieht seine Nachrichten weiter, sonst niemand."""
        p, _ = self._account(args["accountId"])
        mute = bool(args.get("mute", True))
        hours = float(args.get("hours") or 0) or None
        until = self._chat().set_mute(p.name, hours, mute)
        p.tracker.track(p.account_id, tracking.CHAT_MUTED, {"Muted": mute, "Until": until or None,
                                              "Reason": args.get("reason") or ""})
        return {"Muted": mute, "Until": until or None}

    def SetPassword(self, args):
        """Neues Passwort; beendet die laufende Sitzung des Spielers."""
        p, _ = self._account(args["accountId"])
        try:
            self.players.auth.set_password(p.name, str(args["password"]))
        except auth.AuthError:
            raise AdminError(400, f"Password too short (at least {auth.MIN_PASSWORD} characters)")
        self.players.auth.logout(p.name)
        return {"Ok": True}

    def UnlinkSteam(self, args):
        """SteamID vom Account lösen (falsch verknüpft); beendet die Sitzung.
        Ohne Passwort kann sich der Spieler danach nicht mehr anmelden."""
        p, _ = self._account(args["accountId"])
        removed = self.players.auth.unlink_steam(p.name)
        self.players.auth.logout(p.name)
        return {"Removed": removed}

    def SetAdmin(self, args):
        """Webtool-Zugang (admin true/false); verlangt ein Passwort."""
        p, _ = self._account(args["accountId"])
        try:
            self.players.auth.set_admin(p.name, bool(args["admin"]))
        except auth.AuthError:
            raise AdminError(400, "Player has no password yet")
        return {"Admin": self.players.auth.is_admin(p.name)}

    def Logout(self, args):
        """Sitzung beenden: das laufende Spiel fliegt beim nächsten Request
        raus, der Launcher verlangt eine neue Anmeldung."""
        p, _ = self._account(args["accountId"])
        return {"Sessions": self.players.auth.logout(p.name)}

    def GetMaintenance(self, args):
        return {"Maintenance": self.maintenance}

    def SetMaintenance(self, args):
        """Wartungsmodus (wie ServerSettingsManager.SetMaintenance): der
        Launcher zeigt seine Wartungsseite (/launcher/maintenance), die
        Login-Seite nimmt nur noch Admins an. Einschalten beendet alle
        Sitzungen außer denen von Admins (UpdateKickForMaintenance)."""
        on = bool(args["maintenance"])
        self.maintenance = on
        if self.maintenance_path:
            with open(self.maintenance_path, "w", encoding="utf-8") as f:
                json.dump({"Maintenance": on}, f)
        kicked = 0
        if on:
            auth_ = self.players.auth
            for name in set(auth_.sessions.values()):
                if not auth_.is_admin(name):
                    kicked += auth_.logout(name)
        return {"Maintenance": on, "Sessions": kicked}

    def GetNews(self, args):
        return {"News": self._news().all()}

    def SaveNews(self, args):
        return self._news().save(args["title"], args.get("text", ""), args.get("date"),
                              int(args["id"]) if args.get("id") else None)

    def DeleteNews(self, args):
        self._news().delete(int(args["id"]))
        return {}

    def GetBalance(self, args):
        """Stellschrauben der Bling-Wirtschaft (balance.py)."""
        return balance.summary()

    def SetBalance(self, args):
        """Neue Werte {"Enabled", "<Bereich>": {...}}; Werte in ClientKeys
        lassen die Spieler beim nächsten Launcher-Start settings.bin laden."""
        try:
            result = balance.update(args.get("values") or {},
                                    datetime.datetime.now(datetime.UTC)
                                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
        except ValueError as e:
            raise AdminError(400, str(e))
        website.write()  # blings.html mit den neuen Werten
        return result

    # Quellen in der Telemetrie: verdient (positiv) und ausgegeben
    EARN_KINDS: ClassVar[list[str]] = [
        "achievements", "quests", "mine", "loot", "boss", "pillage", "leagues",
        "competitions", "other"]
    SPEND_KINDS: ClassVar[list[str]] = ["bling shop", "game shop"]

    def GetEconomyStats(self, args):
        """Bling-Telemetrie der letzten `days` Tage aus den Kontobüchern
        (Kinds je Buchung, account_state._tag) und der Spielzeit
        (PlaySeconds): Summen je Quelle, Verlauf je Tag, Zeilen je Spieler.
        Buchungen aus dem Webtool (Source admin:…, z. B. Kompensation)
        zählen nicht mit, sie verzerren das Bild."""
        days = max(1, min(90, int(args.get("days") or 7)))
        now = datetime.datetime.now(datetime.UTC)
        since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        totals = {"Earned": {}, "Spent": {}}
        per_day, players = {}, []
        for token, account_id in sorted(self.players.index.items()):
            p = self.players.by_account_id(account_id)
            a = p.state.snapshot()
            earned, spent = {}, {}
            for e in _ledger(p.state.ledger):
                day = e["Time"][:10]
                if day < since or e.get("Source", "").startswith("admin:"):
                    continue
                parts = dict(e.get("Kinds") or {})
                rest = e["Delta"] - sum(parts.values())
                if rest:
                    kind = "other" if rest > 0 else "game shop"
                    parts[kind] = parts.get(kind, 0) + rest
                d = per_day.setdefault(day, {"Earned": 0, "Spent": 0})
                for kind, n in parts.items():
                    side, book = ("Earned", earned) if n > 0 else ("Spent", spent)
                    book[kind] = book.get(kind, 0) + abs(n)
                    totals[side][kind] = totals[side].get(kind, 0) + abs(n)
                    d[side] += abs(n)
            seconds = sum(v for k, v in (a.get(p.state.PLAY_TIME) or {}).items() if k >= since)
            if not (earned or spent or seconds):
                continue
            players.append({
                "Id": account_id, "DisplayName": a.get("DisplayName") or token,
                "Hours": round(seconds / 3600, 1), "Earned": earned, "Spent": spent,
                "Balance": a["Wallet"].get("PremiumCash", 0),
                "LootLeft": loot.bling_loot_left(a, now.timestamp())})
        return {"Days": days, "EarnKinds": self.EARN_KINDS, "SpendKinds": self.SPEND_KINDS,
                "Totals": totals, "Hours": round(sum(x["Hours"] for x in players), 1),
                "PerDay": [dict(v, Day=k) for k, v in sorted(per_day.items())],
                "Players": players, "Enabled": balance.enabled(),
                "LootCap": balance.get("Loot", "WeeklyCap")}

    def GetServerStats(self, args):
        """Technik (server_stats) und Spieler online: LastSeenOnline jünger
        als PLAY_GAP (wird höchstens alle 5 Minuten fortgeschrieben)."""
        now = datetime.datetime.now(datetime.UTC)
        since = (now - datetime.timedelta(seconds=account_state.AccountState.PLAY_GAP)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        online = sum(1 for account_id in self.players.index.values()
                     if (self.players.by_account_id(account_id).state.snapshot()
                         .get("LastSeenOnline") or "") >= since)
        return dict(server_stats.collect(self.players.dir), Online=online,
                    OnlineMinutes=account_state.AccountState.PLAY_GAP // 60)

    def GrantSpecialPack(self, args):
        p, _ = self._account(args["accountId"])
        pack_id = int(args["packId"])
        try:
            special_packs.pack(pack_id)
        except StopIteration:
            raise AdminError(400, f"Unknown pack {pack_id}")
        granted = p.state.grant_special_pack(pack_id)
        for name, fields in p.state.take_pack_events():
            p.notifications.push(name, fields)
        if granted:
            p.tracker.track(p.account_id, 531, {"SpecialPackId": pack_id,
                                                "Reason": args.get("reason") or ""})
        return {"Granted": bool(granted)}

    def call(self, method, query, body):
        """Ergebnis von method; Argumente aus der Query (JsonGet) bzw. dem
        Body (JsonPost, ein Feld je Parameter)."""
        fn = getattr(self, method, None) if method[:1].isupper() else None
        if fn is None:
            raise AdminError(404, f"AdminService.{method} not implemented")
        args = {k: v[0] for k, v in query.items()}
        account_state.SOURCE.value = "admin:" + method  # Kontobuch
        if isinstance(body, dict):
            args.update(body)
        try:
            return _with_item_types(fn(args))
        except (KeyError, ValueError, TypeError) as e:
            raise AdminError(400, f"Invalid arguments for {method}: {e}")


def path_and_query(raw_path):
    """Uri.PathAndQuery wie beim Signieren (ohne Schema/Host)."""
    p = urllib.parse.urlsplit(raw_path)
    return p.path + ("?" + p.query if p.query else "")