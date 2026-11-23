"""Spieler: ein Account je Login-Name.

Der Client schickt sein Sitzungs-Token (Kommandozeile -token, vom Launcher
nach der Anmeldung, auth.py) als Header `t` mit jedem Request; for_token
löst es zum Login-Name auf. Ein neuer Name (Registrierung) legt einen
Account aus der Profil-Vorlage an (wie ein neuer Spieler im Original:
Tutorial von vorne). Zustand je Spieler: Spielstand (AccountState),
Notification-Warteschlange, laufender Angriff.

Ablage in einem Verzeichnis (je Profil, z. B. server/state/new/):
  players.json         {Login-Name: AccountId}
  logins.json          Passwörter und Sitzungen (auth.py)
  <AccountId>.json     Spielstand
Ein Spielstand aus der Zeit vor Multiuser (server/state/account-<profil>.json)
wird beim ersten Start als Name "test" übernommen.
"""

import json
import os
import threading

import attack
import auth
import geoip
import notifications
import tracking
from account_state import AccountState

# Neue Spieler-Ids oberhalb der Burg-Ids (Ubisoft-Burgen < 1000,
# Ersatzspieler ab castles.FAKE_PLAYER_BASE = 1000000)
FIRST_PLAYER_ID = 2000001


class Player:
    def __init__(self, name, state, tracker=None):
        self.name = name  # Login-Name (auch Chat-Benutzer)
        self.state = state
        self.notifications = notifications.NotificationQueue()
        if tracker is not None:  # Ereignisse des Spielers protokollieren (tracking.py)
            state.track = lambda tag, fields: tracker.track(self.account_id, tag, fields)
            self.notifications.on_push = self._track_notification
            self.tracker = tracker
        self.attack = attack.AttackSession()
        self.competitions_week = None  # zuletzt per CompetitionsRefreshed gemeldet
        self.hack_flags = 0  # zuletzt gemeldete ClientHackFlags (anticheat)

    @property
    def account_id(self):
        return self.state.account["AccountId"]

    def _track_notification(self, type_name, fields):
        tag = tracking.notification_tag(type_name, fields)
        if tag:
            self.tracker.track(self.account_id, tag, dict(fields, Notification=type_name))


class Players:
    def __init__(self, directory, profile, log=print, names_as_tokens=False):
        self.dir, self.profile, self.log = directory, profile, log
        self.lock = threading.Lock()
        self.players = {}  # Login-Name -> Player (geladen)
        self.auth = auth.Auth(directory)
        # Entwicklung: Login-Name direkt als -token (Client ohne Launcher)
        self.names_as_tokens = names_as_tokens
        os.makedirs(directory, exist_ok=True)
        self.index_path = os.path.join(directory, "players.json")
        self.tracker = tracking.Tracker(directory)
        if os.path.exists(self.index_path):
            with open(self.index_path, encoding="utf-8") as f:
                self.index = json.load(f)
        else:
            self.index = {}
            self._save_index()

    def _save_index(self):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.index, f, indent=1)
        os.replace(tmp, self.index_path)

    def _path(self, account_id):
        return os.path.join(self.dir, f"{account_id:d}.json")

    def for_token(self, token, ip=None):
        """Spieler zum Sitzungs-Token oder None (unbekannt/abgemeldet)."""
        name = self.auth.resolve(token)
        if name is None and self.names_as_tokens and token in self.index:
            name = token
        return self.get(name, ip) if name is not None else None

    def get(self, token, ip=None):
        """Spieler zum Login-Name; ein neuer Name legt einen Account an, mit
        dem Land der IP des Clients (geoip)."""
        # außerhalb der Sperre: das erste Nachschlagen lädt die Datenbank
        country = geoip.country(ip) if ip and token not in self.index else None
        with self.lock:
            p = self.players.get(token)
            if p is not None:
                return p
            account_id = self.index.get(token)
            is_new = account_id is None
            if is_new:
                account_id = max([FIRST_PLAYER_ID - 1, *self.index.values()]) + 1
                self.index[token] = account_id
                self._save_index()
                self.log(f"[players] neuer Spieler: Token {token!r} -> "
                         f"Account {account_id}")
            state = AccountState(self._path(account_id), log=self.log,
                                 profile=self.profile,
                                 account_id=account_id, country=country)
            p = self.players[token] = Player(token, state, self.tracker)
            if is_new:
                self.tracker.track(account_id, tracking.ACCOUNT_CREATION,
                                   {"CountryCode": country or ""})
            return p

    def by_account_id(self, account_id):
        """Geladener oder gespeicherter Spieler zur AccountId (oder None)."""
        token = next((t for t, a in self.index.items() if a == account_id), None)
        return self.get(token) if token is not None else None

    def delete(self, account_id):
        """Account ohne Helden (Tutorial nie durch) ganz entfernen: Spielstand,
        Bling-Ledger, Login samt Sitzungen und Tracking. Mit Helden nicht:
        dann hängen Freunde, Gilden, Ligen und Replays an der Id."""
        p = self.by_account_id(account_id)
        if p is None:
            raise KeyError(account_id)
        if p.state.snapshot().get("Heroes"):
            raise ValueError("account has heroes")
        self.auth.delete(p.name)
        with self.lock:
            self.players.pop(p.name, None)
            del self.index[p.name]
            self._save_index()
            for path in (p.state.state, p.state.ledger):
                if os.path.exists(path):
                    os.remove(path)
        self.tracker.remove(account_id)
        self.log(f"[players] Account {account_id} ({p.name!r}) gelöscht")

    def all(self):
        return [self.get(t) for t in list(self.index)]