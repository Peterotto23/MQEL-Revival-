"""End-to-end tests: start the game server in an empty player directory and
check login, sanctions, bling shop and web tool over HTTP.

    python3 -m unittest discover tests
"""
# pyright: reportOptionalSubscript=false, reportOptionalMemberAccess=false, reportOptionalIterable=false


import datetime
import glob
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zlib
from typing import ClassVar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import account_state
import anticheat
import auth
import balance
import bling_shop
import castles
import chat_log
import competitions
import free_trials
import game_server
import limits
import loot
import objectives
import patcher
import profiles
import settings_archive
import special_packs
import steam_auth
import trophies
import xmpp_server
from profanity import filter_text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = urllib.request.build_opener(NoRedirect)


def request(url, body=None, headers=None, method=None):
    """(status, headers, body text) without redirects."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data,
        dict(headers or {}, **({"Content-Type": "application/json"} if data else {})),
        method=method,
    )
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "server/game_server.py",
                "--port",
                str(cls.port),
                "--profile",
                "new",
                "--state",
                os.path.join(cls.tmp.name, "state"),
                "--log",
                os.path.join(cls.tmp.name, "log.jsonl"),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(300):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.tmp.cleanup()

    def launcher(self, action, name, password):
        status, _, text = request(
            self.base + "/launcher/" + action, {"name": name, "password": password}
        )
        self.assertEqual(status, 200)
        return json.loads(text)

    def register(self, name, password="geheim123"):
        r = self.launcher("register", name, password)
        self.assertIn("Token", r, r)
        return r["Token"]

    def game(self, token, method="GetAccountInformation"):
        return request(
            self.base + "/AccountService.hqs/" + method,
            headers={"t": token},
            method="GET",
        )

    def admin(self, method, args, cookie=None):
        status, _, text = request(
            self.base + "/admin/api/" + method,
            args,
            {"Cookie": "admin_t=" + cookie} if cookie else None,
        )
        return status, json.loads(text)

    def account_id(self, name, cookie=None):
        _, rows = self.admin("ListAccounts", {}, cookie)
        return next(r["Id"] for r in rows if r["Login"] == name)

    # --- Login --------------------------------------------------------------

    def test_register_and_login(self):
        token = self.register("alice")
        self.assertEqual(len(token), auth.TOKEN_LENGTH)  # the client truncates to 24
        self.assertEqual(self.game(token)[0], 200)
        self.assertEqual(
            self.launcher("register", "ALICE", "x" * 8), {"Error": "name_taken"}
        )
        self.assertEqual(
            self.launcher("login", "alice", "falsch99"), {"Error": "bad_login"}
        )
        self.assertEqual(
            self.launcher("login", "nobody", "geheim123"), {"Error": "bad_login"}
        )
        self.assertEqual(
            self.launcher("register", "a b", "geheim123"), {"Error": "name_invalid"}
        )
        self.assertEqual(
            self.launcher("register", "bob", "kurz"), {"Error": "password_short"}
        )
        # a new login invalidates the old token
        new = self.launcher("login", "alice", "geheim123")["Token"]
        self.assertEqual(self.game(new)[0], 200)
        status, _, text = self.game(token)
        self.assertEqual(status, 401)
        self.assertIn("InvalidConnectionTokenException", text)

    def test_client_start_drops_queued_notifications(self):
        # Notifications from offline time are stored in the account; the
        # client's first request (i=0, at login in parallel to
        # GetAccountInformation) must not deliver them a second time
        alice, bob = self.register("nora"), self.register("otto")
        nora = self.account_id("nora")

        def invite():
            request(
                self.base + "/FriendshipService.hqs/SendFriendshipInvitation",
                {"friendAccountId": nora},
                {"t": bob},
            )

        def notes(i):
            _, _, text = request(
                self.base + "/ServerCommandService.hqs/SendCommands",
                {"commands": []},
                {"t": alice, "i": str(i), "n": "0"},
            )
            return [n["Index"] for n in json.loads(text).get("Notifications", [])]

        invite()
        self.assertEqual(notes(0), [])
        self.assertEqual(notes(1), [])
        invite()  # a new notification during the session arrives, counting from 1
        self.assertEqual(notes(2), [1])

    def test_chat_login_uses_session_token(self):
        token = self.register("chatty")
        _, _, text = request(
            self.base + "/AccountInformationService.hqs/GetAccountInformation",
            {},
            {"t": token, "Host": f"127.0.0.1:{self.port}"},
        )
        info = json.loads(text)["Result"]["ClientSettings"]["XmppInfo"]
        password = auth.chat_password(token)
        self.assertEqual((info["Username"], info["Password"]), ("chatty", password))
        self.assertNotEqual(
            password, token
        )  # sniffing the chat does not reveal the token
        old = xmpp_server.STATE_DIR
        xmpp_server.STATE_DIR = os.path.join(self.tmp.name, "state")
        try:
            self.assertTrue(xmpp_server.session_valid("chatty", password))
            self.assertFalse(xmpp_server.session_valid("chatty", token))
            self.assertFalse(xmpp_server.session_valid("alice", password))
            self.assertFalse(xmpp_server.session_valid("chatty", "x" * 24))
            self.assertFalse(xmpp_server.session_valid("chatty", ""))
        finally:
            xmpp_server.STATE_DIR = old

    def test_chat_moderation(self):
        token = self.register("loudy")
        state = os.path.join(self.tmp.name, "state")
        # the game server writes logins.json into the profile directory
        state = os.path.dirname(
            glob.glob(os.path.join(state, "**", "logins.json"), recursive=True)[0]
        )
        old = xmpp_server.STATE_DIR, xmpp_server.CHAT
        xmpp_server.STATE_DIR, xmpp_server.CHAT = state, chat_log.ChatLog(state)
        sent = []

        class Peer(xmpp_server.Session):
            def __init__(self, user, password):
                super().__init__(
                    None, ("127.0.0.1", 0), os.path.join(self_.tmp.name, "x.log")
                )
                self.user, self.password = user, password

            def send(self, text):
                sent.append((self.user, text))

        self_ = self
        loud = Peer("loudy", auth.chat_password(token))
        other = Peer("other", "")
        room = "en-general-1@conference.x"
        xmpp_server.ROOMS[room] = {loud: ("loudy",), other: ("other",)}

        def say(text):
            sent.clear()
            loud.handle_message(
                f"<message to='{room}' type='groupchat'><body>{text}</body></message>"
            )
            return sorted(u for u, _ in sent)

        try:
            self.assertEqual(say("Hallo+du"), ["loudy", "other"])
            aid = self.account_id("loudy")
            _, d = self.admin("GetChatLog", {"accountId": aid})
            self.assertEqual(
                [(e["Text"], e["AccountId"]) for e in d["Entries"]], [("Hallo du", aid)]
            )
            self.assertEqual(
                self.admin("SetChatMute", {"accountId": aid, "hours": 1})[0], 200
            )
            self.assertEqual(say("nochmal"), ["loudy"])  # only the sender sees it
            _, d = self.admin("GetChatLog", {"text": "NOCH"})
            self.assertTrue(d["Entries"][0]["Muted"])
            self.assertEqual([m["Login"] for m in d["Muted"]], ["loudy"])
            self.admin("SetChatMute", {"accountId": aid, "mute": False})
            self.assertEqual(say("wieder"), ["loudy", "other"])
            # text never ends up in the stanza log
            loud.log("C->S", "<message><body>geheim</body></message>")
            with open(os.path.join(self.tmp.name, "x.log"), encoding="utf-8") as f:
                self.assertNotIn("geheim", f.read())
            # a ban ends the chat session
            self.admin("SetSanction", {"accountId": aid, "status": "ban"})
            with self.assertRaises(ConnectionResetError):
                say("weg")
        finally:
            xmpp_server.ROOMS.pop(room, None)
            xmpp_server.STATE_DIR, xmpp_server.CHAT = old

    def test_chat_log_retention(self):
        with tempfile.TemporaryDirectory() as d:
            log = chat_log.ChatLog(d)
            now = datetime.datetime.now(datetime.UTC)
            log.append(
                "r",
                "a",
                "a",
                "alt",
                now=now - datetime.timedelta(days=chat_log.RETENTION_DAYS + 1),
            )
            log._purged_day = None
            log.append("r", "a", "a", "neu", now=now)
            self.assertEqual([e["Text"] for e in log.search()], ["neu"])

    def test_body_too_large(self):
        with socket.create_connection(("127.0.0.1", self.port), 5) as s:
            s.sendall(
                b"POST /AccountService.hqs/HealthCheck HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 999999999\r\n\r\n"
            )
            self.assertIn(b" 413 ", s.recv(200))

    def test_unknown_token_rejected(self):
        self.assertEqual(self.game("x" * 24)[0], 401)
        self.assertEqual(
            request(
                self.base + "/AccountService.hqs/GetAccountInformation", method="GET"
            )[0],
            401,
        )

    def test_login_body_not_logged(self):
        self.register("secretive", "supergeheim")
        with open(os.path.join(self.tmp.name, "log.jsonl"), encoding="utf-8") as f:
            self.assertNotIn("supergeheim", f.read())

    def test_display_name_codes(self):
        # Codes of the final client (own texts per code), not those of the old DLL
        token = self.register("gina")

        def choose(name):
            status, _, text = request(
                self.base + "/AccountService.hqs/ChooseDisplayName",
                {"displayName": name},
                {"t": token},
            )
            return status, json.loads(text).get("Code") if status != 200 else None

        self.assertEqual(choose("a b")[1], 0x8002010C)
        self.assertEqual(choose("TheAdmin")[1], 0x80020110)
        self.assertEqual(choose("Gina99")[0], 200)
        other = self.register("hank")
        _, _, text = request(
            self.base + "/AccountService.hqs/ChooseDisplayName",
            {"displayName": "gina99"},
            {"t": other},
        )
        self.assertEqual(json.loads(text)["Code"], 0x8002010A)

    # --- Sanctions ----------------------------------------------------------

    def test_ban_and_suspend(self):
        token = self.register("carol")
        aid = self.account_id("carol")
        self.assertEqual(
            self.admin("SetSanction", {"accountId": aid, "status": "ban"})[0], 200
        )
        self.assertEqual(self.game(token)[0], 401)  # a ban ends the session
        self.assertEqual(
            self.launcher("login", "carol", "geheim123"), {"Error": "banned"}
        )
        self.admin("SetSanction", {"accountId": aid, "status": "suspend", "hours": 2})
        r = self.launcher("login", "carol", "geheim123")
        self.assertEqual(r["Error"], "suspended")
        self.assertTrue(r["Until"])
        self.admin("SetSanction", {"accountId": aid, "status": "none"})
        self.assertIn("Token", self.launcher("login", "carol", "geheim123"))

    def test_maintenance(self):
        token = self.register("maint")
        self.assertEqual(
            request(self.base + "/launcher/maintenance", method="GET")[0], 200
        )
        status, r = self.admin("SetMaintenance", {"maintenance": True})
        try:
            self.assertEqual((status, r["Maintenance"]), (200, True))
            self.assertGreaterEqual(r["Sessions"], 1)
            self.assertEqual(
                request(self.base + "/launcher/maintenance", method="GET")[0], 503
            )
            self.assertEqual(self.game(token)[0], 401)  # session ended
            self.assertEqual(
                self.launcher("login", "maint", "geheim123"), {"Error": "maintenance"}
            )
            self.assertEqual(
                self.launcher("register", "frank", "geheim123"),
                {"Error": "maintenance"},
            )
        finally:
            self.admin("SetMaintenance", {"maintenance": False})
        self.assertEqual(
            request(self.base + "/launcher/maintenance", method="GET")[0], 200
        )
        self.assertIn("Token", self.launcher("login", "maint", "geheim123"))

    def test_bling_ledger(self):
        token = self.register("ledger")
        aid = self.account_id("ledger")
        self.admin("UpdateWallet", {"accountId": aid, "PremiumCash": 70})
        request(self.base + f"/blings/buy?t={token}&amount=500", method="GET")
        self.admin("UpdateWallet", {"accountId": aid, "PremiumCash": -20})
        path = os.path.join(self.tmp.name, "state", f"{aid}.blings.jsonl")
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(
            [(r["Delta"], r["Source"]) for r in rows],
            [
                (70, "admin:UpdateWallet"),
                (500, "blings/buy"),
                (-20, "admin:UpdateWallet"),
            ],
        )
        self.assertEqual(rows[-1]["Balance"] - rows[0]["Balance"], 480)

    def test_balance_admin(self):
        status, d = self.admin("GetBalance", {})
        self.assertEqual(
            (status, d["Values"]["Enabled"], d["ClientVersion"]), (200, False, "")
        )
        status, d = self.admin("SetBalance", {"values": {"Mine": {"Capacity": 5000}}})
        self.assertEqual(status, 400)
        # boss raids: seven bosses plus cap, server-only, limits checked
        boss = [f for f in self.admin("GetBalance", {})[1]["Fields"] if f["Section"] == "Boss"]
        self.assertEqual(len(boss), 8)
        self.assertEqual({f["Kind"] for f in boss}, {"server"})
        status, d = self.admin("SetBalance", {"values": {"Boss": {"14": 99999}}})
        self.assertEqual(status, 400)
        status, d = self.admin("SetBalance", {"values": {"Boss": {"14": 12}}})
        self.assertEqual((status, d["Values"]["Boss"]["14"], d["ClientVersion"]), (200, 12, ""))
        self.admin("SetBalance", {"values": {"Boss": {"14": 10}}})
        status, d = self.admin(
            "SetBalance", {"values": {"Enabled": True, "Mine": {"Capacity": 50}}}
        )
        try:
            self.assertEqual((status, d["Values"]["Mine"]["Capacity"]), (200, 50))
            self.assertRegex(d["ClientVersion"], "^B[0-9a-f]{8}$")
        finally:
            self.admin("SetBalance", {"values": {"Enabled": False}})
        self.assertEqual(self.admin("GetBalance", {})[1]["ClientVersion"], "")

    # --- Anti-cheat ---------------------------------------------------------

    def test_client_hack_flags(self):
        token = self.register("mallory")
        aid = self.account_id("mallory")
        # random bits alone are no suspicion, SpeedHackInjected (128) is
        request(
            self.base + "/AccountService.hqs/GetAccountInformation",
            headers={"t": token, "rnd": str(1 | 4 | 1024)},
            method="GET",
        )
        _, rows = self.admin("ListAccounts", {})
        self.assertEqual(next(r for r in rows if r["Id"] == aid)["Cheat"]["Flags"], [])
        request(
            self.base + "/AccountService.hqs/GetAccountInformation",
            headers={"t": token, "rnd": str(128 | 4 | 1024)},
            method="GET",
        )
        _, rows = self.admin("ListAccounts", {})
        self.assertEqual(
            next(r for r in rows if r["Id"] == aid)["Cheat"],
            {"Failures": 0, "Flags": ["SpeedHackInjected"]},
        )

    # --- Bling shop ---------------------------------------------------------

    def test_bling_shop_token_in_url(self):
        token = self.register("dave")
        self.assertEqual(
            request(self.base + "/blings/?t=" + token, method="GET")[0], 200
        )
        _, _, text = request(self.base + "/blings/?t=falsch", method="GET")
        self.assertNotIn("dave", text)

    # --- Web tool -----------------------------------------------------------

    def test_admin_login(self):
        self.register("erin", "adminpass")
        self.register("frank")
        # open without an admin (local network), so the first one can be appointed
        status, _ = self.admin(
            "SetAdmin", {"accountId": self.account_id("erin"), "admin": True}
        )
        self.assertEqual(status, 200)
        try:
            self.assertEqual(self.admin("ListAccounts", {})[0], 401)
            status, headers, _ = request(
                self.base + "/admin/session",
                {"action": "login", "name": "frank", "password": "geheim123"},
            )
            self.assertEqual(status, 401)  # not an admin
            status, headers, text = request(
                self.base + "/admin/session",
                {"action": "login", "name": "erin", "password": "adminpass"},
            )
            self.assertEqual(json.loads(text)["Name"], "erin")
            cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
            # status query of the page (POST without action) does not log out
            status, _, text = request(
                self.base + "/admin/session", {}, {"Cookie": "admin_t=" + cookie}
            )
            self.assertEqual(json.loads(text), {"Name": "erin", "Required": True})
            self.assertEqual(self.admin("ListAccounts", {}, cookie)[0], 200)
            # the game session is unaffected by the web tool login
            self.assertEqual(
                self.launcher("login", "erin", "adminpass").get("Name"), "erin"
            )
            self.assertEqual(self.admin("ListAccounts", {}, cookie)[0], 200)
            # a password change keeps the admin right
            erin = self.account_id("erin", cookie)
            self.admin(
                "SetPassword", {"accountId": erin, "password": "neuespass"}, cookie
            )
            _, d = self.admin("GetAccountDetail", {"accountId": erin}, cookie)
            self.assertTrue(d["IsAdmin"])
        finally:
            _, h, _ = request(
                self.base + "/admin/session",
                {"action": "login", "name": "erin", "password": "neuespass"},
            )
            cookie = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
            self.admin(
                "SetAdmin",
                {"accountId": self.account_id("erin", cookie), "admin": False},
                cookie,
            )
        self.assertEqual(self.admin("ListAccounts", {})[0], 200)


class FreeTrialTest(unittest.TestCase):
    def test_schedule(self):
        self.assertIsNone(free_trials.current(datetime.date(2026, 10, 17)))
        e = free_trials.current(datetime.date(2026, 11, 3))
        self.assertEqual(
            (free_trials.iso(e["Start"]), free_trials.iso(e["End"])),
            ("2026-11-01T00:00:00Z", "2026-11-07T23:59:59Z"),
        )
        self.assertEqual(
            free_trials.from_id(e["Id"]), e
        )  # the Id carries the whole event
        heroes = {
            free_trials.current(datetime.date(2027, m, 1))["Hero"] for m in range(1, 5)
        }
        self.assertEqual(heroes, {2, 3, 4, 5})  # all heroes in turn
        self.assertEqual(
            free_trials.current(datetime.date(2026, 10, 17), forced=5)["Hero"], 5
        )

    def test_grant_reductions_and_buy(self):
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            now = datetime.datetime(2026, 11, 2, 12, tzinfo=datetime.UTC)
            hero_id = free_trials.current(now.date())["Hero"]
            s.account["Heroes"] = [
                h for h in s.account["Heroes"] if h["HeroSpecContainerId"] != hero_id
            ]
            names = [n for n, _ in s.free_trials(now)]
            self.assertEqual(names, ["HeroUnlocked", "FreeTrialActivated"])
            self.assertEqual(s.free_trials(now), [])  # only once per event
            hero = next(
                h for h in s.account["Heroes"] if h["HeroSpecContainerId"] == hero_id
            )
            self.assertEqual(hero["ExpirationDate"], "2026-11-07T23:59:59Z")
            hero["Level"] = 10
            done = [
                f["PriceReductionConditionCompletedId"] for _, f in s.free_trials(now)
            ]
            self.assertEqual(done, [1, 2])  # level 5 and 10
            # purchase at the reduced price (950 - 50 - 100), hero is kept
            sku = next(
                x
                for x in s.data.skus.values()
                if x["ItemType"] == 1
                and x["ItemId"] == hero_id
                and x.get("IsActive")
                and x["Price"]["CurrencyType"] == 1
            )
            s.account["Wallet"]["PremiumCash"] = 1000
            s._buy_hero(sku)
            self.assertEqual(s.account["Wallet"]["PremiumCash"], 1000 - (950 - 150))
            self.assertNotIn("ExpirationDate", hero)
            self.assertEqual(hero["Level"], 10)

    def test_expiry_and_late_buy(self):
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            utc = datetime.UTC
            now = datetime.datetime(2026, 11, 2, 12, tzinfo=utc)
            hero_id = free_trials.current(now.date())["Hero"]
            s.account["Heroes"] = [
                h for h in s.account["Heroes"] if h["HeroSpecContainerId"] != hero_id
            ]
            other = next(h for h in (2, 3, 4, 5) if h != hero_id)
            s.account["Heroes"].append(s._new_hero(other))
            s.free_trials(now)
            s.account["SelectedHeroId"] = hero_id
            hero = next(
                h for h in s.account["Heroes"] if h["HeroSpecContainerId"] == hero_id
            )
            hero["Level"] = 7
            s.expire_trials(now)  # still running
            self.assertIn(hero, s.account["Heroes"])
            later = datetime.datetime(2026, 11, 8, tzinfo=utc)
            self.assertIsNone(free_trials.active(later))
            s.expire_trials(later)
            self.assertNotIn(
                hero_id, [h["HeroSpecContainerId"] for h in s.account["Heroes"]]
            )
            self.assertEqual(s.account["SelectedHeroId"], other)
            # a later purchase at full price brings the hero back with its level
            sku = next(
                x
                for x in s.data.skus.values()
                if x["ItemType"] == 1
                and x["ItemId"] == hero_id
                and x.get("IsActive")
                and x["Price"]["CurrencyType"] == 1
            )
            s.account["Wallet"]["PremiumCash"] = 1000
            s._buy_hero(sku)
            self.assertEqual(s.account["Wallet"]["PremiumCash"], 50)
            back = next(
                h for h in s.account["Heroes"] if h["HeroSpecContainerId"] == hero_id
            )
            self.assertEqual((back["Level"], back.get("ExpirationDate")), (7, None))
            self.assertEqual(s.account[s.EXPIRED_TRIALS], [])

    def test_patcher_versions(self):
        pkgs = patcher.packages(datetime.date(2026, 11, 3))
        self.assertEqual(sorted(p.rm_type for p in pkgs), [2, 7, 8, 10, 11])
        ui = pkgs.pop()
        self.assertEqual(
            len({p.version for p in pkgs}), 1
        )  # data and CRC packages are equal
        version = pkgs[0].version
        # UI patches as whole files, Steam UI as the baseline
        old, new = ui.files("280139"), ui.files(ui.version)
        self.assertRegex(ui.version, r"^280139\.U[0-9a-f]{8}$")
        self.assertEqual(old.keys(), new.keys())
        self.assertTrue(all(old[k] != new[k] for k in old))
        self.assertRegex(version, r"^104956\.Halloween\.T\d+$")
        files = pkgs[1].files(version)[patcher.SETTINGS2]
        self.assertIn(
            b'"Id": %d' % free_trials.current(datetime.date(2026, 11, 3))["Id"],
            settings_archive.read(files)[0],
        )
        self.assertEqual(pkgs[0].files(version), pkgs[0].files("104956.Halloween"))

    def test_client_exe_and_crc_lists(self):
        exe_bytes = b"MZ new exe"
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "mq.exe")
            with open(exe, "wb") as f:
                f.write(exe_bytes)
            patcher.CLIENT_EXE = exe
            try:
                pkgs = {
                    p.rm_type: p for p in patcher.packages(datetime.date(2026, 11, 3))
                }
                game, crc = pkgs[1], pkgs[9]
                self.assertEqual(game.version, f"280139.X{zlib.crc32(exe_bytes):08x}")
                self.assertEqual(crc.version, game.version)
                listed = json.loads(crc.files(crc.version)["crc/GameBinCRC.json"])
                entry = next(
                    e
                    for e in listed["RMPackagesCRCList"][0]["RMPackageFileCRCList"]
                    if e["InstallationRelativePath"] == "MightyQuest.exe"
                )
                self.assertEqual(
                    entry["FileCRCInfo"],
                    {"CRC": zlib.crc32(exe_bytes), "FileSize": len(exe_bytes)},
                )
                # Steam state: list unchanged
                self.assertEqual(
                    crc.files("280139")["crc/GameBinCRC.json"],
                    patcher._read(
                        os.path.join(
                            patcher.ROOT, "server", "data", "crc", "GameBinCRC.json"
                        )
                    ),
                )
                data_crc = pkgs[7].files(pkgs[7].version)["crc/DataBinCRC.json"]
                boot = pkgs[2].files(pkgs[2].version)["Bin/Boot.tsc"]
                self.assertIn(b'"CRC":%d' % zlib.crc32(boot), data_crc)
                # older own exe (unknown state): whole exe and list
                self.assertEqual(game.installed_files("280139.X00000000"), {})
                self.assertIsNotNone(crc.installed_files("280139.X00000000"))
            finally:
                patcher.CLIENT_EXE = None


class BalanceTest(unittest.TestCase):
    """Bling mine via balance.py: values, loot limit, patcher versions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        balance.load(os.path.join(self.tmp.name, "balance.json"))

    def tearDown(self):
        balance.load(os.path.join(self.tmp.name, "missing.json"))
        balance._data = {"Values": balance.defaults(), "History": {}, "Changed": None}
        self.tmp.cleanup()

    def mine_castle(self, amount):
        return {
            "Level": 10,
            "_MineStatuses": {"7": {"CumulatedAmount": amount}},
            "Rooms": [
                {"Buildings": [{"Id": 7, "SpecContainerId": balance.BLING_MINE}]}
            ],
        }

    def test_off_is_original(self):
        self.assertEqual(castles.mine_rank(16, 1)["Capacity"], 25)
        self.assertEqual(loot.stealable_buildings(self.mine_castle(20), 0, 0)[7][1], 20)
        self.assertEqual(balance.client_version(), "")

    def test_mine_rules_and_patch(self):
        today = datetime.date(2026, 3, 10)
        before = next(p for p in patcher.packages(today) if p.rm_type == 2).version
        balance.update({"Enabled": True}, "2026-03-10T00:00:00Z")
        spec = castles.mine_rank(16, 1)
        self.assertEqual((spec["ProductionPeriod"], spec["Capacity"]), ("4h", 42))
        self.assertEqual(loot.stealable_buildings(self.mine_castle(20), 0, 0)[7][1], 1)
        balance.update({"Mine": {"ProductionHours": 1, "StealHours": 3}}, "x")
        self.assertEqual(loot.stealable_buildings(self.mine_castle(20), 0, 0)[7][1], 3)
        after = next(p for p in patcher.packages(today) if p.rm_type == 2).version
        self.assertEqual(after, before + "." + balance.client_version())
        files_on = patcher._data_bin_files(after)[patcher.SETTINGS_BIN]
        files_off = patcher._data_bin_files(before)[patcher.SETTINGS_BIN]
        self.assertEqual(files_off, patcher._settings_bin(""))
        content = settings_archive.read(files_on)[0]
        i = content.index(balance.MINE_FILE)
        self.assertIn(b'"ProductionPeriod":"1h"', content[i : i + 3000])
        self.assertIn(b'"Capacity":42,', content[i : i + 3000])
        s = content.index(b'"Code":"15522"')
        self.assertRegex(content[s : s + 300], rb'"Amount":500\b')
        self.assertEqual(
            balance.sku_price(account_state.GameData().skus["15522"])["Amount"], 500
        )
        o = content.index(balance.OBJECTIVE_FILE)
        text = content[o : content.index(b"\x00", o + len(balance.OBJECTIVE_FILE))]
        blings = re.findall(rb'"PremiumCash",\s*"Amount":(\d+)', text)
        # the seven region bosses ramp up by 50 per region
        boss_ids = (5, 100, 101, 99, 103, 102, 104)  # Snottingham .. Dragonificus
        self.assertEqual(
            [balance.OBJECTIVE_BLINGS[i] for i in boss_ids],
            [50, 100, 150, 200, 250, 300, 350],
        )
        # in the file the amounts follow the order of ObjectiveSettings.json
        self.assertEqual(
            blings,
            [
                str(balance.OBJECTIVE_BLINGS.get(oid, original)).encode()
                for oid, _, original in balance.objectives()
            ],
        )
        self.assertEqual(objectives.rewards(304)[0]["PremiumCash"], 150)
        o = content.index(balance.LEAGUE_FILE)
        text = content[o : content.index(b"\x00", o + len(balance.LEAGUE_FILE))]
        self.assertIn(b'"Amount":200', text)  # league 6, rank 1: 100 x 200 %
        self.assertEqual(
            competitions.reward(1, 5)["RewardItems"][0]["CurrencyAmount"]["Amount"], 50
        )
        # the old state stays known (deltas from there), an unknown one does not
        self.assertIsNotNone(patcher._version_parts(after))
        self.assertIsNone(patcher._version_parts(before + ".Bdeadbeef"))

    def test_bling_loot(self):
        account, now = {}, 1_000_000
        loot_table = {1: {"Gold": 0}, 2: {"Gold": 0}}
        loot.add_bling_drop(loot_table, account, now)  # off: nothing
        self.assertFalse(any("PremiumCash" in x for x in loot_table.values()))
        balance.update(
            {"Enabled": True, "Loot": {"Chance": 100, "Amount": 400, "WeeklyCap": 600}},
            "x",
        )
        loot.add_bling_drop(loot_table, account, now)
        self.assertEqual(sum(x.get("PremiumCash", 0) for x in loot_table.values()), 400)
        loot.record_bling_loot(account, 400, now)
        table2 = {1: {}}
        loot.add_bling_drop(table2, account, now + 60)
        self.assertEqual(table2[1]["PremiumCash"], 200)  # remainder up to the limit
        loot.record_bling_loot(account, 200, now + 60)
        table3 = {1: {}}
        loot.add_bling_drop(table3, account, now + 120)
        self.assertNotIn("PremiumCash", table3[1])
        # free again after 7 days, old entries drop out
        later = now + loot.BLING_LOOT_WINDOW + 61
        self.assertEqual(loot.bling_loot_left(account, later), 600)
        loot.record_bling_loot(account, 0, later)
        self.assertEqual(account[loot.BLING_LOOT], [])

    def test_boss_raid_bounty(self):
        account, now = {}, 2_000_000
        self.assertEqual(loot.boss_bounty(account, 14, now), 0)  # off
        balance.update(
            {"Enabled": True, "Boss": {"14": 10, "20": 70, "WeeklyCap": 100}}, "x"
        )
        self.assertEqual(loot.boss_bounty(account, 14, now), 10)
        self.assertEqual(loot.boss_bounty(account, 20, now), 70)
        self.assertEqual(loot.boss_bounty(account, 5, now), 0)  # no boss castle
        # incremental defaults: 7 bosses, ascending
        pay = [balance.defaults()["Boss"][str(c)] for c in balance.BOSS_CASTLES]
        self.assertEqual(pay, sorted(pay))
        self.assertEqual(len(set(pay)), 7)
        # rolling weekly cap of 100: 70 paid, then only the remainder
        loot.record_boss_bounty(account, 70, now)
        self.assertEqual(loot.boss_bounty(account, 20, now + 60), 30)
        loot.record_boss_bounty(account, 30, now + 60)
        self.assertEqual(loot.boss_bounty(account, 20, now + 120), 0)
        later = now + loot.BLING_LOOT_WINDOW + 61
        self.assertEqual(loot.boss_bounty(account, 20, later), 70)
        loot.record_boss_bounty(account, 0, later)
        self.assertEqual(account[loot.BOSS_LOG], [])
        balance.update({"Boss": {"WeeklyCap": 0}}, "x")  # 0 = unlimited
        self.assertEqual(loot.boss_bounty(account, 20, now + 120), 70)
        with self.assertRaises(ValueError):
            balance.update({"Boss": {"14": -1}}, "x")

    def test_boss_raid_payout_end_to_end(self):
        import attack

        st = account_state.AccountState(
            os.path.join(self.tmp.name, "a.json"),
            log=lambda m: None,
            profile="new",
            account_id=7,
        )
        balance.update({"Enabled": True, "Boss": {"15": 20, "WeeklyCap": 50}}, "x")

        def raid(castle_id, completion=0, **over):
            suspicious = over.pop("suspicious", False)
            session = attack.AttackSession()
            cur = {
                "castle": castles.find_castle(castle_id), "hero": None, "hero_level": 1,
                "attack_id": "x", "started": None, "loot": {}, "treasure": (0, 0),
                "items": {}, "treasure_item": None, "buildings": {},
                "material_mines": {}, "trap_items": {}, "validation": False,
                "practice": False, "delegated": False, "completion_reward": [],
                "attack_type": 0, "competition": 0, "revenge": None,
                "targeted": False, "boosts": {}, "shielded": False, "par": 60.0,
                "trophies": (0, 0, 0),
            }
            session.current = {**cur, **over}
            payload, gains = attack.end_attack(
                st.snapshot(), {"CompletionType": completion}, session,
                suspicious=suspicious,
            )
            return payload, gains

        before = st.account["Wallet"]["PremiumCash"]
        payload, gains = raid(15)
        self.assertEqual(gains["boss_bounty"], 20)
        self.assertEqual(payload["TotalPremiumCash"], 20)  # shown in the report
        changed = st.credit_attack(gains)
        self.assertEqual(changed["wallet"]["PremiumCash"], 20)
        self.assertEqual(st.account["Wallet"]["PremiumCash"], before + 20)
        with open(st.ledger, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readlines()[-1])["Kinds"], {"boss": 20})
        # repeatable: the same boss pays again, then the cap (50) bites
        for expected in (20, 10, 0):
            _, gains = raid(15)
            self.assertEqual(gains["boss_bounty"], expected)
            st.credit_attack(gains)
        self.assertEqual(st.account["Wallet"]["PremiumCash"], before + 50)
        balance.update({"Boss": {"WeeklyCap": 0}}, "x")
        # no payout for a lost raid, practice, a flagged fight or a non-boss castle
        self.assertEqual(raid(15, completion=1)[1]["boss_bounty"], 0)
        self.assertEqual(raid(15, practice=True)[1]["boss_bounty"], 0)
        self.assertEqual(raid(15, suspicious=True)[1]["boss_bounty"], 0)
        self.assertEqual(raid(15, competition=3)[1]["boss_bounty"], 0)
        self.assertEqual(raid(1)[1]["boss_bounty"], 0)
        balance.update({"Enabled": False}, "x")
        self.assertEqual(raid(15)[1]["boss_bounty"], 0)  # off = original rules

    def test_bling_shop_pack(self):
        st = account_state.AccountState(
            os.path.join(self.tmp.name, "a.json"),
            log=lambda m: None,
            profile="new",
            account_id=7,
        )
        balance.update({"Enabled": True}, "x")
        self.assertNotIn("EarlyHeroAccess", special_packs.pack(66))
        heroes = len(st.account["Heroes"])
        st.account["Wallet"]["PremiumCash"] = 3999
        self.assertEqual(st.buy_special_pack(66, 4000), "blings")
        st.account["Wallet"]["PremiumCash"] = 4100
        self.assertEqual(st.buy_special_pack(66, 4000), "ok")
        self.assertEqual(st.account["Wallet"]["PremiumCash"], 100)
        self.assertEqual(len(st.account["Heroes"]), heroes)  # no hero access
        events = st.take_pack_events()
        self.assertEqual(events[0][0], "WalletUpdated")
        self.assertTrue(
            any(e[0] in ("HeroInventoryAdded", "BuyBackAdded") for e in events)
        )
        self.assertEqual(st.buy_special_pack(66, 4000), "owned")
        self.assertEqual(
            dict(bling_shop.offers(None)).get(67), None
        )  # only during the event
        self.assertEqual(dict(bling_shop.offers("Halloween"))[67], 1500)

    def test_ledger_kinds_and_play_time(self):
        st = account_state.AccountState(
            os.path.join(self.tmp.name, "a.json"),
            log=lambda m: None,
            profile="new",
            account_id=7,
        )
        balance.update({"Enabled": True, "Achievements": {"1": 30}}, "x")
        st.account["CompletedAchievements"] = [1]
        with st.lock:
            st._check_achievements()
            st._save()
        with open(st.ledger, encoding="utf-8") as f:
            last = json.loads(f.readlines()[-1])
        self.assertEqual(last["Kinds"], {"achievements": 30})
        t = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=datetime.UTC)
        st.seen(t)
        st.seen(t + datetime.timedelta(minutes=6))  # counts
        st.seen(t + datetime.timedelta(minutes=60))  # break, does not count
        self.assertEqual(st.account[st.PLAY_TIME], {"2026-10-17": 360})

    def test_objective_blings_off(self):
        self.assertEqual(objectives.rewards(100)[0]["PremiumCash"], 50)

    def test_achievement_blings(self):
        st = account_state.AccountState(
            os.path.join(self.tmp.name, "a.json"),
            log=lambda m: None,
            profile="new",
            account_id=7,
        )
        st.account["CompletedAchievements"] = [1, 2]
        st._check_achievements()
        self.assertEqual(st.account["Wallet"]["PremiumCash"], 0)  # off: nothing
        before = st.account["Wallet"]["PremiumCash"]
        balance.update({"Enabled": True, "Achievements": {"2": 100}}, "x")
        st.events = []
        st._check_achievements()  # retroactively for already earned ones
        self.assertEqual(st.account["Wallet"]["PremiumCash"] - before, 10 + 100)
        self.assertEqual(st.events[-1][0], "WalletUpdated")
        st.events = []
        st._check_achievements()  # only once
        self.assertEqual(st.account["Wallet"]["PremiumCash"] - before, 110)
        self.assertEqual(st.events, [])

    def test_achievement_ui_patch(self):
        import ui_patches

        off = ui_patches.patched(patcher.GAME_DATA)
        self.assertNotIn(
            b"achievementBlings", off["Data/UI/Js/hyperquest.extensions.js"]
        )
        self.assertNotIn(b"achievementBlings", off["Data/UI/Html/de/TopView.html"])
        # 10th trial hero: the buy button goes straight to the purchase with trial discount
        self.assertIn(
            b"skuCode={{:SkuCode}}{{if !HeroModel.IsHeroOwned && HeroModel.IsInFreeTrial}}"
            b'&isPremiumCashConfirmationPurchase=1{{/if}}" class="buy"',
            off["Data/UI/Html/en/TopView.html"],
        )
        v_off = patcher._ui_version()
        balance.update({"Enabled": True, "Achievements": {"1": 0, "2": 33}}, "x")
        on = ui_patches.patched(patcher.GAME_DATA)
        js = on["Data/UI/Js/hyperquest.extensions.js"]
        self.assertIn(b'"2": 33', js)
        self.assertNotIn(b'"1": 0', js)
        self.assertIn(
            b"~achievementBlings(Achievement.Id)", on["Data/UI/Html/de/TopView.html"]
        )
        # old UI state: all files anew instead of no patch at all
        self.assertNotEqual(patcher._ui_version(), v_off)
        self.assertEqual(patcher._game_ui_files(v_off), {})


class LeagueBonusTest(unittest.TestCase):
    def test_attack_bonus(self):
        self.assertEqual(trophies.attack_bonus({}), 0.0)
        self.assertAlmostEqual(
            trophies.attack_bonus({"LeagueId": 4, "SubLeagueId": 1}), 0.09
        )


class ShieldTest(unittest.TestCase):
    def test_new_shield_replaces_old(self):
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            now = datetime.datetime.now(datetime.UTC)
            first = s.add_shield(3600, now)[-1][1]["ExpirableId"]
            out = s.add_shield(3600, now)
            self.assertEqual(out[0], ("ShieldExpired", {"ExpirableId": first}))
            shields = [
                x for x in s.account["Expirables"] if x["ExpirableType"] == "ShieldEnd"
            ]
            self.assertEqual(
                [x["Id"] for x in shields],
                [s.account["BuildInfo"]["ShieldEndExpirableId"]],
            )


class MaterialMineTest(unittest.TestCase):
    def test_levels_theft_and_harvest(self):
        import attack
        import item_drops

        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            room = {
                "Buildings": [
                    {"Id": 999, "Rank": 1, "SpecContainerId": loot.MATERIAL_MINE}
                ]
            }
            s.account["BuildInfo"]["Draft"]["Rooms"].append(room)
            # state from before the ingredients: full pack without a timer
            status = {
                "CumulatedAmount": 10,
                "IsActive": True,
                "IsDestroyed": False,
                "ProductionExpirableId": account_state.NO_EXPIRABLE,
                "ShieldExpirableId": account_state.NO_EXPIRABLE,
            }
            s.account["BuildInfo"].setdefault("MineStatuses", {})["999"] = status
            now = datetime.datetime.now(datetime.UTC)
            basic = set(item_drops.material_templates("Basic"))

            def rolled():
                return sum(
                    i["TemplateId"] not in basic for i in status["CumulatedHeroItems"]
                )

            s.material_mines(now)
            self.assertEqual(
                (status["Productionlevel"], len(status["CumulatedHeroItems"])), (1, 10)
            )
            for day, want in [(1, 2), (2, 4), (5, 10), (9, 10)]:
                s.material_mines(now + datetime.timedelta(hours=23 * day + 1))
                self.assertEqual(rolled(), want)
            self.assertEqual(status["Productionlevel"], 6)
            self.assertEqual(
                status["ProductionExpirableId"], account_state.NO_EXPIRABLE
            )

            c = {"Level": 5, "Rooms": [room], "_MineStatuses": {"999": dict(status)}}
            offer = loot.stealable_materials(c)
            self.assertEqual((len(offer[999][0]), offer[999][1]), (2, False))
            _, lost, destroyed = attack._pillaged_materials(
                {"material_mines": offer},
                [{"CastleBuildingId": 999, "IsDestroyed": True}],
            )
            s._pillage_material_mines(
                s.account["BuildInfo"]["MineStatuses"],
                {
                    "material_mines": lost,
                    "material_destroyed": destroyed,
                    "mine_shield": 3600,
                },
                set(),
                now,
            )
            self.assertEqual(status["CumulatedAmount"], 8)
            self.assertNotEqual(status["ShieldExpirableId"], account_state.NO_EXPIRABLE)
            self.assertNotEqual(
                status["ProductionExpirableId"], account_state.NO_EXPIRABLE
            )

            s.events = []
            s._cmd_HarvestMineBuildingCommand(
                {"MineBuildingId": 999, "CurrencyType": 0}
            )
            result = s.events[0][1]["Result"]
            self.assertEqual(
                (result["EntityHarvestType"], len(result["HarvestedHeroItems"])),
                ("CraftingMaterialMine", 8),
            )
            self.assertEqual(
                (status["Productionlevel"], status["CumulatedHeroItems"]), (1, [])
            )


class BeltTest(unittest.TestCase):
    def test_belts_follow_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            s = account_state.AccountState(path, log=lambda m: None, profile="new")
            hero = s._new_hero(2)
            s.account["Heroes"].append(hero)
            tid = hero["EquippedConsumables"][0]["TemplateId"]
            s.account["Inventory"]["HeroItems"].append(
                {
                    "SlotIndex": 99,
                    "StackCount": 3,
                    "Item": {
                        "$type": account_state.T("HeroConsumableItem"),
                        "TemplateId": tid,
                    },
                }
            )
            hero["EquippedConsumables"][0]["StackCount"] = 0  # stale belt
            s._save()
            self.assertEqual(hero["EquippedConsumables"][0]["StackCount"], 3)
            # potion in combat: the client learns the new stack count
            ability = s.data.consumables[tid]["AbilityId"]
            self.assertEqual(
                s._use_consumables(2, [ability]),
                [("HeroInventoryUpdated", {"SlotIndex": 99, "StackCount": 2})],
            )
            self.assertEqual(
                s._use_consumables(2, [ability, ability]),
                [("HeroInventoryRemoved", {"SlotIndex": 99})],
            )

    def test_belt_refills_other_potion(self):
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            hero = s._new_hero(2)
            hero["Level"] = 5
            s.account["Heroes"].append(hero)
            hero["EquippedConsumables"][0]["TemplateId"] = 2
            HC = account_state.T("HeroConsumableItem")
            for idx, tid, n in (
                (98, 2, 1),
                (99, 1, 4),
                (97, 3, 5),
            ):  # 3 only from level 9
                s.account["Inventory"]["HeroItems"].append(
                    {
                        "SlotIndex": idx,
                        "StackCount": n,
                        "Item": {"$type": HC, "TemplateId": tid},
                    }
                )
            s._save()
            events = s._use_consumables(2, [s.data.consumables[2]["AbilityId"]])
            self.assertEqual(
                [e[0] for e in events],
                [
                    "HeroInventoryRemoved",
                    "HeroConsumableUnequip",
                    "HeroConsumableEquip",
                ],
            )
            self.assertEqual(events[2][1]["ConsumableItem"]["TemplateId"], 1)
            self.assertEqual(events[2][1]["ConsumableItem"]["StackCount"], 4)
            # potion 1 still in stock: no switch
            self.assertEqual(
                [
                    e[0]
                    for e in s._use_consumables(2, [s.data.consumables[1]["AbilityId"]])
                ],
                ["HeroInventoryUpdated"],
            )


class EquipTest(unittest.TestCase):
    def _state(self, d):
        s = account_state.AccountState(
            os.path.join(d, "a.json"), log=lambda m: None, profile="new"
        )
        hero = s._new_hero(2)  # Knight, level 1
        s.account["Heroes"].append(hero)
        return s, hero

    def _bag(self, s, idx, item):
        s.account["Inventory"]["HeroItems"].append(
            {
                "$type": account_state.T("AccountInventorySlot"),
                "SlotIndex": idx,
                "StackCount": 1,
                "Item": item,
            }
        )

    def test_default_equipment_never_in_bag(self):
        with tempfile.TemporaryDirectory() as d:
            s, hero = self._state(d)
            self._bag(s, 5, s._item_from_template(1, level=1))  # Longsword
            s._cmd_HeroEquipmentEquipCommand(
                {"HeroId": 2, "SourceSlotId": 5, "DestinationSlot": 8}
            )
            self.assertEqual(hero["Equipment"]["MainHand"]["TemplateId"], 1)
            self.assertEqual(s._slots(), [])  # the default weapon is discarded
            with self.assertRaises(ValueError):  # the default shoulders stay on
                s._cmd_HeroEquipmentUnequipCommand(
                    {"HeroId": 2, "SourceSlot": 2, "DestinationSlotId": 6}
                )
            # legacy data: default item in the bag, slot empty -> back to the hero
            self._bag(s, 7, hero["Equipment"]["Shoulders"])
            hero["Equipment"]["Shoulders"] = None
            self.assertTrue(s._drop_default_equipment())
            self.assertEqual(hero["Equipment"]["Shoulders"]["TemplateId"], 132)
            self.assertEqual(s._slots(), [])

    def test_equip_checks_level_and_class(self):
        with tempfile.TemporaryDirectory() as d:
            s, _ = self._state(d)
            self._bag(s, 5, s._item_from_template(1, level=10))
            self._bag(s, 6, s._item_from_template(37, level=1))  # type 9: not a Knight
            for idx in (5, 6):
                with self.assertRaises(ValueError):
                    s._cmd_HeroEquipmentEquipCommand(
                        {"HeroId": 2, "SourceSlotId": idx, "DestinationSlot": 8}
                    )
            self.assertEqual(len(s._slots()), 2)  # nothing lost


class HeroLevelTest(unittest.TestCase):
    def test_admin_raises_level(self):
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            s.account["Heroes"].append(s._new_hero(5))
            hero, _, changed = s.admin_hero_level(5, 5)
            self.assertEqual((hero["Level"], changed), (5, True))
            self.assertEqual(hero["XP"], loot.xp_per_level()[4])
            self.assertEqual(
                s.admin_hero_level(5, 3)[1:], (0, False)
            )  # never downwards


class BoostTest(unittest.TestCase):
    def test_activation_frees_slot_in_client(self):
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            s.account["Inventory"]["HeroItems"].append(
                {
                    "SlotIndex": 7,
                    "StackCount": 1,
                    "Item": {
                        "$type": account_state.T("HeroConsumableItem"),
                        "TemplateId": 1116,
                    },
                }
            )
            s.events = []
            s._cmd_ActivateConsumableCommand(
                {"ConsumableTemplateId": 1116, "InventorySlotIndex": 7}
            )
            self.assertEqual(
                [n for n, _ in s.events],
                ["HeroInventoryRemoved", "ConsumableActivated", "ExpirableAdded"],
            )
            self.assertNotIn(7, s._occupied())


class LimitsTest(unittest.TestCase):
    def test_login_throttle(self):
        with tempfile.TemporaryDirectory() as d:
            a = auth.Auth(d)
            a.set_password("dave", "geheim123")
            for _ in range(limits.MAX_FAILURES):
                self.assertFalse(a.check("dave", "falsch", "10.0.0.1"))
            # locked, even with the right password and from another IP
            with self.assertRaises(auth.AuthError):
                a.check("dave", "geheim123", "10.0.0.2")
            with self.assertRaises(auth.AuthError):
                a.check("other", "x", "10.0.0.1")
            a.set_password("eve", "geheim123")
            self.assertTrue(a.check("eve", "geheim123", "10.0.0.3"))

    def test_connection_limit(self):
        c = limits.ConnectionLimit(2)
        self.assertTrue(c.acquire("1.2.3.4") and c.acquire("1.2.3.4"))
        self.assertFalse(c.acquire("1.2.3.4"))
        self.assertTrue(c.acquire("5.6.7.8"))
        c.release("1.2.3.4")
        self.assertTrue(c.acquire("1.2.3.4"))


class SteamAuthTest(unittest.TestCase):
    ID = "76561198000000001"

    def test_new_steam_account_has_no_password(self):
        with tempfile.TemporaryDirectory() as d:
            a = auth.Auth(d)
            proof = a.steam_proof(self.ID)
            self.assertEqual(a.steam_proof_id(proof), self.ID)
            a.steam_create("Hero", proof)
            self.assertEqual(a.by_steam(self.ID), "Hero")
            # the proof is used up; without a password no login by name
            self.assertIsNone(a.steam_proof_id(proof))
            self.assertTrue(a.has_password("Hero"))
            self.assertFalse(a.check("Hero", "geheim123", "10.0.0.1"))
            self.assertFalse(a.check("Hero", "", "10.0.0.1"))
            # survives a restart; the admin can set a password
            self.assertEqual(auth.Auth(d).by_steam(self.ID), "Hero")
            a.set_password("Hero", "geheim123")
            self.assertTrue(a.check("Hero", "geheim123"))
            self.assertEqual(a.by_steam(self.ID), "Hero")

    def test_link_existing_account(self):
        with tempfile.TemporaryDirectory() as d:
            a = auth.Auth(d)
            a.set_password("old", "geheim123")
            a.link_steam("old", a.steam_proof(self.ID))
            self.assertEqual(a.by_steam(self.ID), "old")
            self.assertTrue(a.check("old", "geheim123"))
            # one SteamID belongs to one account and vice versa
            a.set_password("other", "geheim123")
            with self.assertRaises(auth.AuthError):
                a.link_steam("other", a.steam_proof(self.ID))
            with self.assertRaises(auth.AuthError):
                a.link_steam("old", a.steam_proof("76561198000000002"))
            self.assertEqual(a.by_steam(self.ID), "old")
            a.delete("old")
            self.assertIsNone(a.by_steam(self.ID))

    def test_proof_unknown_or_expired(self):
        with tempfile.TemporaryDirectory() as d:
            a = auth.Auth(d)
            a.set_password("old", "geheim123")
            for proof in ("", "erfunden"):
                with self.assertRaises(auth.AuthError):
                    a.link_steam("old", proof)
                with self.assertRaises(auth.AuthError):
                    a.steam_create("neu", proof)
            proof = a.steam_proof(self.ID)
            a.steam_proofs[proof] = (
                self.ID,
                a.steam_proofs[proof][1] - auth.STEAM_PROOF_S - 1,
            )
            with self.assertRaises(auth.AuthError):
                a.steam_create("neu", proof)
            # an existing name is not overwritten
            with self.assertRaises(auth.AuthError):
                a.steam_create("old", a.steam_proof(self.ID))

    def test_account_name(self):
        free = lambda n: False
        name = steam_auth.account_name
        self.assertEqual(name("Testuser", self.ID, free), "Testuser")
        self.assertEqual(name("Jörg der Große!", self.ID, free), "Jorg_der_Groe")
        self.assertEqual(name("日本語", self.ID, free), "Steam_000001")
        self.assertEqual(name("", self.ID, free), "Steam_000001")
        self.assertEqual(name("ab", self.ID, free), "Steam_000001")
        taken = {"hero", "hero_2"}
        self.assertEqual(name("Hero", self.ID, lambda n: n.lower() in taken), "Hero_3")
        long = name("x" * 40, self.ID, free)
        self.assertTrue(auth.NAME_RE.fullmatch(long), long)
        self.assertTrue(
            auth.NAME_RE.fullmatch(name("y" * 40, self.ID, lambda n: n == "y" * 20))
        )

    def test_ticket_checks(self):
        for ticket in ("", "xyz", "12"):
            with self.assertRaises(steam_auth.SteamError):
                steam_auth.verify(ticket)

    def test_ticket_only_once(self):
        ticket = "0123456789abcdef" * 4
        steam_auth._use_once(ticket)
        with self.assertRaises(steam_auth.SteamError) as e:
            steam_auth._use_once(ticket.upper())
        self.assertEqual(str(e.exception), "steam_reused")
        steam_auth._use_once("fedcba9876543210" * 4)

    def test_global_call_limit(self):
        saved = list(steam_auth._calls)
        try:
            steam_auth._calls[:] = [time.monotonic()] * steam_auth.MAX_CALLS_PER_MIN
            with self.assertRaises(steam_auth.SteamError) as e:
                steam_auth._count_call()
            self.assertEqual(str(e.exception), "steam_busy")
            self.assertEqual(steam_auth.persona(self.ID), "")  # without a call
            steam_auth._calls[:] = [
                time.monotonic() - 61
            ] * steam_auth.MAX_CALLS_PER_MIN
            steam_auth._count_call()
        finally:
            steam_auth._calls[:] = saved

    def test_profane_persona(self):
        if steam_auth.profanity.PATTERN is None:
            self.skipTest("no profanity list")
        bad = next(
            w
            for w in ("fuck", "shit", "cunt")
            if steam_auth.profanity.filter_text(w) != w
        )
        self.assertEqual(
            steam_auth.account_name(bad, self.ID, lambda n: False), "Steam_000001"
        )
        self.assertEqual(
            steam_auth.account_name(bad + " you", self.ID, lambda n: False),
            "Steam_000001",
        )

    def test_unlink_and_password_flags(self):
        with tempfile.TemporaryDirectory() as d:
            a = auth.Auth(d)
            a.steam_create("Hero", a.steam_proof(self.ID))
            self.assertFalse(a.has_hash("Hero"))
            with self.assertRaises(auth.AuthError):  # an admin needs a password
                a.set_admin("Hero", True)
            self.assertEqual(a.steam_id("Hero"), self.ID)
            self.assertTrue(a.unlink_steam("Hero"))
            self.assertFalse(a.unlink_steam("Hero"))
            self.assertIsNone(a.by_steam(self.ID))
            self.assertIsNone(auth.Auth(d).by_steam(self.ID))

    def test_tickets_not_logged(self):
        qs = "steamID=7656&ticket=14000000ABCD&locale=en&steamTicket=99&x"
        red = game_server.redact_query(qs)
        self.assertNotIn("ABCD", red)
        self.assertNotIn("=99", red)
        self.assertIn("steamID=7656", red)
        self.assertIn("locale=en", red)
        line = "GET /launcher/?steamID=1&ticket=14000000ABCD HTTP/1.1"
        self.assertNotIn(
            "ABCD", game_server.SECRET_QUERY_RE.sub(r"\1=(redacted)", line)
        )


class AdminWebAccessTest(unittest.TestCase):
    NETS: ClassVar[list] = [ipaddress.ip_network("93.184.0.0/16")]

    def test_local_always(self):
        for ip in ("127.0.0.1", "192.168.0.10", "10.1.2.3"):
            self.assertTrue(game_server.admin_web_allowed(ip, False, False, []))

    def test_external_only_listed_net_tls_with_admin(self):
        allowed = game_server.admin_web_allowed
        self.assertTrue(allowed("93.184.2.7", True, True, self.NETS))
        self.assertFalse(allowed("93.184.2.7", False, True, self.NETS))  # plain text
        self.assertFalse(allowed("93.184.2.7", True, False, self.NETS))  # no admin
        self.assertFalse(allowed("8.8.8.8", True, True, self.NETS))  # foreign network


class CastleTest(unittest.TestCase):
    def test_limited_decoration_quota(self):
        # "[CE]" decoration (quota from the castle level packs) counts when
        # placed; it used to be buyable again after every login
        with tempfile.TemporaryDirectory() as d:
            s = account_state.AccountState(
                os.path.join(d, "a.json"), log=lambda m: None, profile="new"
            )
            s.account[s.SALE] = (
                1  # starter castle 1: level 1 brings 4 decoration quotas
            )
            mod = next(
                m
                for m in s.shop_sku_modifiers()
                if m["LimitedQuantityMax"]
                and s.data.skus[m["SkuCode"]]["ItemType"] == 13
            )
            sku = s.data.skus[mod["SkuCode"]]
            field = account_state.CURRENCY[sku["Price"]["CurrencyType"]]
            s.account["Wallet"][field] = 10**6
            room = 1
            s.account["BuildInfo"]["Draft"]["Rooms"].append({"Id": room})

            def place(i):
                s._cmd_AddCastleDecorationCommand(
                    {
                        "SkuCode": sku["Code"],
                        "SpecContainerId": sku["ItemId"],
                        "RoomId": room,
                        "Id": 9000 + i,
                    }
                )

            for i in range(mod["LimitedQuantityMax"]):
                place(i)
            with self.assertRaises(ValueError):
                place(99)
            again = next(
                m for m in s.shop_sku_modifiers() if m["SkuCode"] == sku["Code"]
            )
            self.assertEqual(
                again["LimitedQuantityConsumed"], mod["LimitedQuantityMax"]
            )

    def test_huge_account_id(self):
        # used to recurse until a stack overflow (SendFriendshipInvitation)
        self.assertIsNone(castles.find_castle(10**18))
        self.assertIsNone(profiles.fake_account(10**18))

    def test_tutorial_castle_until_assignment_150(self):
        # Assignment 150 aligns the selection to castle 3; hidden afterwards
        def ids(done):
            r = castles.attack_selection(10, done, 3)
            return {
                c["DefenderAccountSummary"]["Id"]
                for g in r["CastlesByLevel"]
                for c in g["Castles"]
            }

        self.assertIn(3, ids([]))
        self.assertNotIn(3, ids([150]))


class AntiCheatTest(unittest.TestCase):
    CUR: ClassVar[dict] = {
        "treasure": (100, 50),
        "buildings": {7: ("InGameCoin", 30, False, False)},
    }

    def test_honest(self):
        params = {
            "AttackFrameCount": 300,
            "AttackFrameDuration": 0.0333,
            "TreasureRoomOpenFrameCount": 250,
            "KilledCreatureIds": [1, 2],
            "TreasureRoomLootedGold": 100,
            "CastleRating": 5,
            "PillagedMines": [
                {"CastleBuildingId": 7, "StolenAmount": 30},
                {"CastleBuildingId": 9, "StolenAmount": 0},
            ],
        }
        cur = dict(self.CUR, started=time.monotonic() - 11)
        self.assertEqual(anticheat.end_attack_violations(params, cur), [])

    def test_violations(self):
        params = {
            "AttackFrameCount": 300,
            "AttackFrameDuration": 0.0333,
            "TreasureRoomOpenFrameCount": 301,
            "LootedGoldCreatureIds": [1, 1],
            "TreasureRoomLootedGold": 101,
            "CastleRating": 6,
            "PillagedMines": [
                {"CastleBuildingId": 7, "StolenAmount": 31},
                {"CastleBuildingId": 8, "StolenAmount": 5},
            ],
        }
        # a fight of 10 s game time ended one second after the start
        cur = dict(self.CUR, started=time.monotonic() - 1)
        self.assertEqual(len(anticheat.end_attack_violations(params, cur)), 7)


class ProfanityTest(unittest.TestCase):
    def test_filter(self):
        self.assertEqual(filter_text("what the fuck!"), "what the ****!")
        self.assertEqual(filter_text("Du Arschloch"), "Du *********")
        self.assertEqual(filter_text("sh1t"), "****")
        # whole words only, harmless words of other languages stay
        self.assertEqual(filter_text("Assassin con amigo"), "Assassin con amigo")

    def test_chat_body(self):
        # the client sends the text URL-encoded
        m = xmpp_server.re.match(
            r"<body>(.*)</body>", "<body>Hallo+Claude+%3C3+shit</body>"
        )
        self.assertEqual(
            xmpp_server._chat_body(m),
            "<body>Hallo Claude &lt;3 shit</body>"
            "<bodyfiltered>Hallo Claude &lt;3 ****</bodyfiltered>",
        )


if __name__ == "__main__":
    unittest.main()