"""Anmeldung: Login-Name + Passwort -> Sitzungs-Token.

Ersetzt den Ubisoft-/SleepyGiant-Login des Originals. Die Login-Seite des
Launchers (patcher.login_page) prüft Name und Passwort hier und bekommt
ein zufälliges Token, das der Launcher als Cookie t hält und dem Spiel als
-token mitgibt; der Spielserver nimmt nur Tokens an, die hier ausgegeben
wurden. Eine neue Anmeldung macht das alte Token ungültig (wie
KickedForMultiConnection im Original).

Ablage logins.json im Profilverzeichnis:
  {"users": {Name: {"Salt", "Hash"[, "Admin": true]}}, "sessions": {Token: Name}}
Admins melden sich mit demselben Passwort am Webtool an (admin_login); deren
Sitzungen liegen nur im Speicher und stören die Spielsitzung nicht.
Spieler aus der Zeit vor der Anmeldung (Name in players.json, aber ohne
Passwort) legen ihr Passwort beim ersten Anmelden fest.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

import limits

# scrypt-Parameter (RFC 7914, interaktive Anmeldung)
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)

NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{3,24}")
MIN_PASSWORD = 6
# Der Client kürzt -token auf 24 Zeichen (Original: GUID als Base64);
# 18 Bytes ergeben genau 24 Zeichen
TOKEN_BYTES = 18
TOKEN_LENGTH = 24
STEAM_PROOF_S = 600  # Gültigkeit des Beweises einer geprüften SteamID
STEAM_ID_RE = re.compile(r"7656\d{13}")  # SteamID64 of an individual account


def parse_steam_id(text):
    """SteamID64 from a bare ID or a steamcommunity.com/profiles/<id> URL,
    else None (vanity URLs /id/<name> cannot be resolved offline)."""
    text = str(text or "").strip()
    m = re.search(r"steamcommunity\.com/profiles/(\d+)", text)
    if m:
        text = m.group(1)
    return text if STEAM_ID_RE.fullmatch(text) else None


def chat_password(token):
    """Passwort für den Chat (XmppInfo.Password) zum Sitzungs-Token: der
    Chat läuft ohne TLS, wer mitliest, soll damit keine Spielsitzung
    übernehmen können. Einweg-Hash des zufälligen Tokens, gleich lang wie
    dieses (der Client kürzt Tokens auf 24 Zeichen)."""
    digest = hashlib.sha256(b"mqel-chat:" + token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")[:TOKEN_LENGTH]


class AuthError(Exception):
    """Fehler mit Schlüssel für die Login-Seite (dort übersetzt)."""


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


class Auth:
    def __init__(self, directory):
        self.path = os.path.join(directory, "logins.json")
        self.lock = threading.Lock()
        self.users, self.sessions = {}, {}
        self.steam_proofs = {}  # Beweis -> (SteamID, Zeitpunkt), nur im Speicher
        self.admin_sessions = {}  # Webtool: Token -> Name, nur im Speicher
        self.throttle = limits.FailureThrottle()  # Passwort-Raten bremsen
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.users, self.sessions = data.get("users", {}), data.get("sessions", {})
            # zu lange Tokens (vor TOKEN_BYTES) kämen gekürzt an: verwerfen
            self.sessions = {t: n for t, n in self.sessions.items() if len(t) == TOKEN_LENGTH}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"users": self.users, "sessions": self.sessions}, f, indent=1)
        os.replace(tmp, self.path)

    def has_password(self, name):
        """Hat der Name einen Login-Eintrag (Passwort oder SteamID)? Dann
        legt die erste Anmeldung kein Passwort mehr fest."""
        return name in self.users

    def has_hash(self, name):
        """Ist ein Passwort gesetzt (Steam-Accounts haben keins)?"""
        return "Hash" in self.users.get(name, {})

    def set_password(self, name, password):
        if len(password) < MIN_PASSWORD:
            raise AuthError("password_short")
        salt = secrets.token_bytes(16)
        with self.lock:
            self.users[name] = dict(self.users.get(name, {}), Salt=salt.hex(),
                                    Hash=_hash(password, salt).hex())
            self._save()

    def check(self, name, password, ip=None):
        """Passwort richtig? Nach zu vielen Fehlversuchen für den Namen oder
        die IP AuthError("too_many_attempts"), ohne zu rechnen (scrypt ist
        teuer)."""
        keys = ("name:" + name, "ip:" + ip if ip else None)
        if self.throttle.blocked(*keys):
            raise AuthError("too_many_attempts")
        u = self.users.get(name)
        # Steam-Account ohne Passwort (steam_create): nur per Ticket anmelden
        ok = u is not None and "Hash" in u and hmac.compare_digest(
            _hash(password, bytes.fromhex(u["Salt"])).hex(), u["Hash"])
        if not ok:
            self.throttle.fail(*keys)
        return ok

    def login(self, name):
        """Neues Sitzungs-Token; ältere Tokens des Namens verfallen."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self.lock:
            self.sessions = {t: n for t, n in self.sessions.items() if n != name}
            self.sessions[token] = name
            self._save()
        return token

    # --- Steam --------------------------------------------------------------
    # Ein Account hat höchstens eine SteamID (users[Name]["SteamId"]), eine
    # SteamID gehört zu höchstens einem Account. Ein Steam-Account ohne
    # Passwort hat keinen Hash (der Webtool-Admin kann eines setzen).

    def by_steam(self, steam_id):
        """Login-Name zur SteamID oder None."""
        with self.lock:
            return self._by_steam(steam_id)

    def _by_steam(self, steam_id):  # unter self.lock
        return next((n for n, u in self.users.items()
                     if u.get("SteamId") == steam_id), None)

    def steam_id(self, name):
        return self.users.get(name, {}).get("SteamId")

    def unlink_steam(self, name):
        """SteamID vom Account lösen (Webtool); True, wenn eine da war."""
        with self.lock:
            had = self.users.get(name, {}).pop("SteamId", None) is not None
            if had:
                self._save()
            return had

    def link_steam_admin(self, name, steam_id, force=False):
        """Link steam_id to the account `name` without a login proof (web
        tool). Returns the name of the account the SteamID was taken from,
        or None. Without force, a SteamID that belongs to another account
        or an account that already has a different SteamID is refused
        (AuthError steam_taken / steam_other)."""
        with self.lock:
            user = self.users.setdefault(name, {})
            if user.get("SteamId") not in (None, steam_id) and not force:
                raise AuthError("steam_other")
            owner = self._by_steam(steam_id)
            moved = None
            if owner not in (None, name):
                if not force:
                    raise AuthError("steam_taken")
                self.users[owner].pop("SteamId", None)
                moved = owner
            user["SteamId"] = steam_id
            self._save()
            return moved

    def steam_proof(self, steam_id):
        """Beweis für die Login-Seite, dass der Server diese SteamID geprüft
        hat (zufällig, nur im Speicher, STEAM_PROOF_S gültig); damit
        verknüpft ein Altspieler seinen Account (link_steam) oder legt ein
        neuer Spieler seinen an (steam_create)."""
        proof = secrets.token_urlsafe(18)
        now = time.monotonic()
        with self.lock:
            self.steam_proofs = {p: (s, t) for p, (s, t) in self.steam_proofs.items()
                                 if now - t < STEAM_PROOF_S}
            self.steam_proofs[proof] = (steam_id, now)
        return proof

    def steam_proof_id(self, proof):
        """SteamID zum Beweis oder None (unbekannt/abgelaufen)."""
        entry = self.steam_proofs.get(proof or "")
        if entry and time.monotonic() - entry[1] < STEAM_PROOF_S:
            return entry[0]
        return None

    def link_steam(self, name, proof):
        """Account name (mit Passwort) mit der SteamID des Beweises
        verknüpfen; der Beweis verfällt."""
        with self.lock:
            steam_id = self.steam_proof_id(proof)
            if steam_id is None or name not in self.users:
                raise AuthError("steam_expired")
            if self._by_steam(steam_id) not in (None, name):
                raise AuthError("steam_taken")
            if self.users[name].get("SteamId", steam_id) != steam_id:
                raise AuthError("steam_other")
            self.users[name]["SteamId"] = steam_id
            self.steam_proofs.pop(proof, None)
            self._save()

    def steam_create(self, name, proof):
        """Neuer Steam-Account ohne Passwort; der Name muss frei sein
        (Aufrufer prüft). Der Beweis verfällt."""
        with self.lock:
            steam_id = self.steam_proof_id(proof)
            if steam_id is None or name in self.users:
                raise AuthError("steam_expired")
            if self._by_steam(steam_id) is not None:
                raise AuthError("steam_taken")
            self.users[name] = {"SteamId": steam_id}
            self.steam_proofs.pop(proof, None)
            self._save()

    def logout(self, name):
        """Alle Sitzungen des Namens beenden (laufendes Spiel fliegt raus)."""
        with self.lock:
            before = len(self.sessions)
            self.sessions = {t: n for t, n in self.sessions.items() if n != name}
            if len(self.sessions) != before:
                self._save()
            return before - len(self.sessions)

    def delete(self, name):
        """Login und Sitzungen des Namens entfernen (Account gelöscht)."""
        with self.lock:
            self.users.pop(name, None)
            self.sessions = {t: n for t, n in self.sessions.items() if n != name}
            self._save()

    def resolve(self, token):
        """Login-Name zum Token oder None."""
        return self.sessions.get(token or "")

    # --- Webtool ------------------------------------------------------------

    def is_admin(self, name):
        return bool(self.users.get(name, {}).get("Admin"))

    def has_admins(self):
        return any(u.get("Admin") for u in self.users.values())

    def set_admin(self, name, admin):
        """Admin-Recht setzen; nur mit Passwort (sonst keine Anmeldung)."""
        with self.lock:
            if not self.has_hash(name):  # Webtool-Anmeldung braucht ein Passwort
                raise AuthError("no_password")
            if admin:
                self.users[name]["Admin"] = True
            else:
                self.users[name].pop("Admin", None)
                self.admin_sessions = {t: n for t, n in self.admin_sessions.items() if n != name}
            self._save()

    def admin_login(self, name, password, ip=None):
        """Webtool-Token für einen Admin oder None (AuthError nach zu vielen
        Fehlversuchen)."""
        if not self.is_admin(name):
            self.throttle.fail("ip:" + ip if ip else None)
            return None
        if not self.check(name, password, ip):
            return None
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.admin_sessions[token] = name
        return token

    def admin_logout(self, token):
        with self.lock:
            self.admin_sessions.pop(token or "", None)

    def admin_resolve(self, token):
        """Admin zum Webtool-Token oder None (Recht inzwischen entzogen: None)."""
        name = self.admin_sessions.get(token or "")
        return name if name and self.is_admin(name) else None
