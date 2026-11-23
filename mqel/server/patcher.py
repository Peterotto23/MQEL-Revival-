"""Patcher-Dienst und Login-Seite für den PublicLauncher.

Der Launcher (client/Launcher/PublicLauncher.exe) fragt beim Start
{PatcherServiceUrl}/PatcherService.hqs/GetRMLauncherAndPackagesVersion
mit seinen installierten Paketen (RMClientPackagesVersion, aus
LauncherData/InstalledPackages.json) und bekommt je Paket einen
RMPackagePatch. Mit CanInstallPatch lädt er PatchInstallUrl (ein .7z) nach
..\\Temp, entpackt es und wendet jede <Pfad>.xdelta per
"xdelta3 -d -f -s <Datei>" auf die Datei unter GameData\\Bin (Pakettyp
GameBin) bzw. GameData an; .new/.replace/.delete gibt es auch. Danach
trägt er die Version in InstalledPackages.json ein (TaskApplyGamePatches,
0x4e36a0, Patch 0x4e238e). Spieldaten liefert der Server nie aus, nur
Deltas auf die Dateien der Steam-Installation.

Antworten sind nackte DTOs (ohne WrappedResponse wie beim Spiel), Enums als
Zahlen (der Leser 0x432270 nimmt nur Ziffern), Typnamen aus den Serializern des Launchers.

Pakete hier:
  DataBin (Name ""): Delta auf Bin/Boot.tsc: Filtergruppe des aktuellen
    Saison-Events statt "OrdinaryDay" (Event-Optik der Lobby/Burgen; der
    Client wählt das Event aus den aktiven Gruppen, 0x109c010, s.
    SeasonalOverridesSettings). Version 104956 = Steam, 104956.<Gruppe>.

  CommunityEventSettings (Name ""): Delta auf
    Data/GameplaySettings/settings2.bin (Archiv der CommunityEvent-JSONs,
    settings_archive.py): zum Event ein zusätzlicher SkuCommunityEvent mit
    den Saison-Artikeln des Shops. Die stehen im Katalog ohne IsActive; der
    Client schaltet Artikel aus SkuCommunityEvents im Zeitfenster frei (so
    liefen auch die Daily Deals). Der Account-Feld ShopSkus liest der finale
    Client nicht. Versionen wie DataBin.

  GameUI (8): die UI-Patches aus ui_patches.py als ganze Dateien
    (.replace), Version 280139.U<CRC>.

  GameBin (1, nur mit --client-exe) und die Prüfsummenlisten der
    geänderten Pakete: s. CRC_LISTS.

Die Login-Seite (LauncherWebSiteUrl) setzt die Cookies, die die Launcher-
Oberfläche aus dem iframe liest (t = Spieler-Token, email,
hyperquest_launcher_session); der Launcher startet das Spiel dann mit
-token/-email/-server_url.
"""

import datetime
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import zlib

import balance
import free_trials
import settings_archive
import ui_patches
from loot import ROOT, SETTINGS

BIN = os.path.join(ROOT, "client", "GameData", "Bin")
BACKGROUND = os.path.join(ROOT, "client", "Launcher", "DefaultBackground.png")

# RMLauncherPatchFlags/RMPackagePatchFlags (0x418df6)
CAN_INSTALL_PATCH, UP_TO_DATE, UPDATE_VERSION_ONLY = 2, 4, 16
# RMPackageType (InstalledPackages.json der Steam-Installation): GameBin 1,
# DataBin 2, DataConfig 3, DataBF 4 (je Bigfile), DataCRC 7, GameUI 8,
# GameCRC 9, CommunityEventSettings 10, ...CRC 11
GAME_BIN, DATA_BIN, GAME_UI, COMMUNITY_EVENT_SETTINGS = 1, 2, 8, 10
DATA_VERSION = "104956"  # GameData-Stand der Steam-Installation
GAME_VERSION = "280139"  # GameBin (MightyQuest.exe) der Steam-Installation

# Prüfsummenlisten der Pakete (..\\GameData\\crc, je eigenes Paket): der
# Launcher prüft damit die Dateien (TaskRepair) und stürzt an einer Datei
# ab, die zu keinem Paket passt (getauschte Exe, 0x4d91a1). Jedes hier
# geänderte Paket bringt daher seine Liste mit den neuen Werten mit.
# Die Listen stehen in keinem Archiv; Vorlage sind die einer
# Steam-Installation (server/data/crc/, Stand 104956/280139).
CRC_LISTS = {GAME_BIN: (9, "crc/GameBinCRC.json"),        # GameCRC
             DATA_BIN: (7, "crc/DataBinCRC.json"),        # DataCRC
             COMMUNITY_EVENT_SETTINGS: (11, "crc/CommunityEventSettingsCRC.json")}

# per game_server.py --client-exe verteilte Spiel-Exe (gepatchte
# MightyQuest_profile.exe); ohne bleibt GameBin unangetastet
CLIENT_EXE = None
LAUNCHER_VERSION = "276072"  # version.PublicLauncher.txt
PUBLICATION_LABEL = "0.36.1.34.0"

# Saison-Events (Filtergruppe aus SeasonalOverridesSettings), jedes Jahr
# wiederkehrend: (Gruppe, Beginn (Monat, Tag), Ende einschließlich).
# Ostern hängt am Ostersonntag (s. easter_window).
EVENTS = [
    ("Halloween", (10, 20), (11, 3)),
    ("Christmas", (12, 15), (12, 30)),
    ("NewYearEve", (12, 31), (1, 2)),
    ("ValentinesDay", (2, 10), (2, 16)),
]


def easter_sunday(year):
    """Ostersonntag (gregorianisch, Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


# per game_server.py --seasonal-event erzwungenes Event (sonst nach Datum)
FORCED_EVENT = None


def seasonal_event(today, forced=None):
    """Filtergruppe des Events am Tag `today`, sonst "OrdinaryDay"."""
    forced = forced or FORCED_EVENT
    if forced:
        return forced
    md = (today.month, today.day)
    for group, start, end in EVENTS:
        if start <= end and start <= md <= end or start > end and (md >= start or md <= end):
            return group
    sunday = easter_sunday(today.year)
    if sunday - datetime.timedelta(days=7) <= today <= sunday + datetime.timedelta(days=7):
        return "Easter"
    return "OrdinaryDay"


def boot_tsc(group):
    """Boot.tsc des Clients mit `group` statt der Alltagsgruppe."""
    with open(os.path.join(BIN, "Boot.tsc"), encoding="latin-1", newline="") as f:
        src = f.read()
    line = 'AddEnableGroup "OrdinaryDay"'
    if src.count(line) != 1:
        raise RuntimeError(f"Boot.tsc: Zeile {line} nicht gefunden")
    return src.replace(line, f'AddEnableGroup "{group}"').encode("latin-1")


def _seven_zip():
    """7z im PATH, sonst die 7z.exe des Launchers (Windows-Entwicklung ohne
    installiertes 7-Zip)."""
    found = shutil.which("7z")
    if found:
        return found
    bundled = os.path.join(ROOT, "client", "Launcher", "7z.exe")
    if os.path.exists(bundled):
        return bundled
    raise RuntimeError("7z fehlt (Paket p7zip-full)")


@functools.lru_cache(maxsize=8)
def _archive(files):
    """.7z mit den Dateien ((Pfad im Archiv, Inhalt), ...) bauen."""
    seven_zip = _seven_zip()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        for path, data in files:
            dest = os.path.join(src, *path.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        out = os.path.join(tmp, "p.7z")
        subprocess.run([seven_zip, "a", "-bd", "-y", out, "."], cwd=src, check=True,
                       stdout=subprocess.DEVNULL)
        with open(out, "rb") as f:
            return f.read()


def _varint(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | n & 0x7F)
        n >>= 7
    return bytes(reversed(out))


def vcdiff(source, target):
    """Delta im VCDIFF-Format (RFC 3284), das xdelta3 -d -s <source>
    anwendet: gemeinsamer Anfang und Schluss als COPY aus der Quelle,
    dazwischen ADD. Standard-Codetabelle: 1 = ADD, 19 = COPY Modus 0
    (VCD_SELF, absolute Adresse), Größe jeweils folgend."""
    prefix = 0
    limit = min(len(source), len(target))
    while prefix < limit and source[prefix] == target[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < limit - prefix
           and source[-1 - suffix] == target[-1 - suffix]):
        suffix += 1
    data, inst, addr = bytearray(), bytearray(), bytearray()
    if prefix:
        inst += bytes([19]) + _varint(prefix)
        addr += _varint(0)
    middle = target[prefix:len(target) - suffix]
    if middle:
        inst += bytes([1]) + _varint(len(middle))
        data += middle
    if suffix:
        inst += bytes([19]) + _varint(suffix)
        addr += _varint(len(source) - suffix)
    body = (_varint(len(target)) + bytes([0]) + _varint(len(data))
            + _varint(len(inst)) + _varint(len(addr)) + data + inst + addr)
    return (b"\xd6\xc3\xc4\x00\x00"  # Magic, Version 0, keine Header-Optionen
            + bytes([1]) + _varint(len(source)) + _varint(0)  # VCD_SOURCE
            + _varint(len(body)) + body)


_PATCHES = {}  # (Typ, Name, von, nach) -> Archiv; 7z der Exe dauert


class Package:
    """Ein vorhandenes Paket der Steam-Installation, das nur Deltas auf
    seine Dateien bekommt. files(version) -> {Pfad im Paket: Inhalt},
    None für unbekannte Versionen (dann kein Patch). Mit replace=True
    kommen ganze Dateien (<Pfad>.replace: Launcher löscht die alte und
    benennt um, 0x4e2d72), unabhängig vom installierten Inhalt. Mit
    any_installed=True (nur bei replace) bekommt auch ein unbekannter Stand
    alle Dateien, z. B. eine ältere --client-exe."""

    def __init__(self, rm_type, name, version, files, replace=False, any_installed=False):
        self.rm_type, self.name, self.version, self.files = rm_type, name, version, files
        self.replace, self.any_installed = replace, replace and any_installed

    def installed_files(self, installed):
        """Dateien des installierten Stands; None = kein Patch möglich."""
        files = self.files(installed)
        return {} if files is None and self.any_installed else files

    @property
    def key(self):
        return self.rm_type, self.name

    def patch_name(self, installed):
        return f"{self.rm_type:d}{self.name}_{installed}_{self.version}.7z"

    def patch(self, installed):
        """Archiv mit <Pfad>.xdelta je Datei (Launcher: TaskApplyGamePatches
        Inkrementell, 0x4e238e)."""
        key = self.key + (installed, self.version)
        if key not in _PATCHES:
            old, new = self.installed_files(installed), self.files(self.version)
            if old is None or new is None:  # Aufrufer prüfen vorher; nie raten
                raise ValueError(f"kein Patch möglich: {self.key} {installed} -> {self.version}")
            if self.replace:
                files = tuple((path + ".replace", data) for path, data in sorted(new.items())
                              if old.get(path) != data)
            else:
                files = tuple((path + ".xdelta", vcdiff(old[path], data))
                              for path, data in sorted(new.items()))
            if len(_PATCHES) >= 16:
                _PATCHES.pop(next(iter(_PATCHES)))
            _PATCHES[key] = _archive(files)
        return _PATCHES[key]


@functools.lru_cache(maxsize=64)
def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _game_bin_files(version):
    """GameBin-Versionen: 280139 = Steam-Exe, 280139.X<CRC> = CLIENT_EXE."""
    if version == GAME_VERSION:
        return {"MightyQuest.exe": _read(os.path.join(BIN, "MightyQuest.exe"))}
    if CLIENT_EXE and version == _game_version():
        return {"MightyQuest.exe": _read(CLIENT_EXE)}
    return None


GAME_DATA = os.path.join(ROOT, "client", "GameData")


def _ui_version():
    """GameUI mit den UI-Patches (ui_patches.py): 280139.U<CRC32>."""
    crc = 0
    for path, data in sorted(ui_patches.patched(GAME_DATA).items()):
        crc = zlib.crc32(data, zlib.crc32(path.encode(), crc))
    return f"{GAME_VERSION}.U{crc:08x}"


def _game_ui_files(version):
    """GameUI-Versionen: 280139 = Steam-UI, 280139.U<CRC> = gepatcht.
    Nur die gepatchten Dateien; der Rest von Data/UI bleibt unberührt."""
    patched = ui_patches.patched(GAME_DATA)
    if version == GAME_VERSION:
        return {path: _read(str(ui_patches.original(
            ui_patches.pathlib.Path(GAME_DATA, *path.split("/"))))) for path in patched}
    if version == _ui_version():
        return patched
    if version.startswith(GAME_VERSION + ".U"):
        # älterer UI-Patch-Stand (Regeln oder Achievement-Blings geändert):
        # Inhalt unbekannt, das .replace-Paket bringt dann alle Dateien;
        # mit None bekäme der Client nie wieder neue UI-Patches
        return {}
    return None


def _game_version():
    if not CLIENT_EXE:
        return GAME_VERSION
    return f"{GAME_VERSION}.X{zlib.crc32(_read(CLIENT_EXE)):08x}"


def crc_list(path, files):
    """Prüfsummenliste `path` (unter GameData, Vorlage server/data/crc) mit CRC32 und Größe der
    Dateien `files` ({Pfad im Paket: Inhalt}); Text sonst unverändert."""
    text = _read(os.path.join(ROOT, "server", "data", *path.split("/")))
    for name, data in files.items():
        entry = json.dumps(name.replace("/", "\\")).encode()
        pattern = re.compile(rb'("InstallationRelativePath":' + re.escape(entry)
                             + rb',\s*"FileCRCInfo":\{\s*"CRC":)\d+(,\s*"FileSize":)\d+')
        crc, size = zlib.crc32(data), len(data)
        text, n = pattern.subn(
            lambda m, crc=crc, size=size: b"%s%d%s%d" % (m.group(1), crc, m.group(2), size),
            text)
        if n != 1:
            raise ValueError(f"{path}: {name} {n:d}-mal statt einmal")
    return text


def _crc_package(pkg):
    """Paket der Prüfsummenliste zu `pkg`, gleiche Versionen."""
    rm_type, path = CRC_LISTS[pkg.rm_type]

    def files(version):
        data = pkg.files(version)
        return None if data is None else {path: crc_list(path, data)}
    return Package(rm_type, pkg.name, pkg.version, files, replace=True,
                   any_installed=pkg.any_installed)


SETTINGS_BIN = "Data/GameplaySettings/settings.bin"


@functools.lru_cache(maxsize=4)
def _settings_bin(crc):
    """settings.bin zum Balancing-Stand B<crc> (balance.py), "" = Original."""
    original = _read(os.path.join(ROOT, "client", "GameData", *SETTINGS_BIN.split("/")))
    if not crc:
        return original
    try:
        return balance.settings_bin(original, balance.history(crc))
    except Exception as e:  # noqa: BLE001 - Boundary: a bad Economy patch must not block every launcher
        # Without this, one mismatch in settings.bin makes GetRMLauncherAndPackagesVersion
        # answer 500 for every player. Serve the original file instead and say so loudly.
        # (lru_cache keeps this result until the server restarts.)
        print(f"[patcher] !!! settings.bin B{crc} could not be built, serving the ORIGINAL: {e!r}",
              flush=True)
        return original


def _data_bin_files(version):
    """DataBin-Versionen: 104956 = Steam, 104956.<Gruppe> = Saison-Event
    (ein Trial-Teil ändert hier nichts), .B<CRC> = Balancing-Werte in
    settings.bin. settings.bin ist immer dabei (sonst bliebe beim Ausschalten
    die geänderte Datei liegen)."""
    parts = _version_parts(version)
    if parts is None:
        return None
    return {"Bin/Boot.tsc": boot_tsc(parts[0]), SETTINGS_BIN: _settings_bin(parts[2])}


# Saison-Gruppe -> Stichworte in InternalDescription der Shop-Artikel
EVENT_SKU_KEYWORDS = {
    "Halloween": ("halloween",),
    "Christmas": ("christmas", "xmas"),
    "NewYearEve": ("christmas", "xmas"),
    "ValentinesDay": ("valentine",),
    "Easter": ("easter",),
}
SETTINGS2 = "Data/GameplaySettings/settings2.bin"
EVENT_SKU_ID = 9000  # Id des zusätzlichen SkuCommunityEvent


def event_sku_codes(group):
    """Kaufbare Shop-Artikel (mit Preis, ohne IsActive) des Events."""
    with open(os.path.join(SETTINGS, "ShopSettings", "ShopSkuBaseSettings.json"),
              encoding="utf-8-sig") as f:
        skus = json.load(f)["Skus"]
    words = EVENT_SKU_KEYWORDS.get(group, ())
    return sorted((s["Code"] for s in skus
                   if s.get("Price") and not s.get("IsActive")
                   and any(w in s.get("InternalDescription", "").lower() for w in words)),
                  key=int)


FREE_TRIAL_FILE = b"COMMUNITYEVENTSETTINGS\\COMMUNITYEVENTFREETRIALSETTINGS.JSON\x00"


@functools.lru_cache(maxsize=8)
def settings2(group, trial_id=None):
    """settings2.bin mit dem Saison-SkuCommunityEvent (Zeitfenster offen:
    das Paket gilt nur, solange das Event läuft; Ende vor 2038, der Client
    rechnet die Zeiten in 32 Bit) und dem Helden-Trial `trial_id`
    (free_trials.event_id) statt des Archer-Trials von 2015."""
    trial = free_trials.from_id(trial_id) if trial_id else None
    with open(os.path.join(ROOT, "client", "GameData", *SETTINGS2.split("/")), "rb") as f:
        blob = f.read()
    codes = event_sku_codes(group)
    if not codes and not trial:
        return blob
    content = settings_archive.read(blob)[0][:-4]  # ohne CRC
    end = content.rindex(b"\r\n    ]\r\n}\x00")  # Ende von SkuCommunityEvents
    if codes:
        entry = json.dumps({"SkuCodes": codes, "Id": EVENT_SKU_ID, "DebugName": group,
                            "EventStartTime": "2000-01-01T00:00:00Z",
                            "EventEndTime": "2037-12-31T23:59:59Z"})
        content = content[:end] + b",\r\n        " + entry.encode() + content[end:]
    start = end
    if trial:  # Datei-Inhalt "PFAD\0JSON\0" des Trials ersetzen
        start = content.index(FREE_TRIAL_FILE) + len(FREE_TRIAL_FILE)
        stop = content.index(b"\x00", start)
        content = (content[:start] + free_trials.settings_json(trial).encode()
                   + content[stop:])
    return settings_archive.replace_tail(blob, start, content[start:])


def _trial_version(trial):
    """Versionsteil eines Trials: T<Event-Id> (free_trials.event_id, nur
    Ziffern wie die übrigen Versionsnamen)."""
    return f"T{trial['Id']:d}"


def _version_parts(version):
    """(Saison-Gruppe, Trial-Event oder None, Balancing-CRC oder "") aus
    104956[.<Gruppe>][.T<Id>][.B<CRC>]; None, wenn die Version nicht von
    hier stammt oder der Balancing-Stand unbekannt ist."""
    base, *parts = version.split(".")
    if base != DATA_VERSION:
        return None
    group, trial, crc = "OrdinaryDay", None, ""
    for part in parts:
        if part[:1] == "B" and len(part) == 9:
            if balance.history(part[1:]) is None:
                return None
            crc = part[1:]
            continue
        if part[:1] == "T" and part[1:].isdigit():
            try:
                trial = free_trials.from_id(int(part[1:]))
            except ValueError:
                return None
        else:
            group = part
    return group, trial, crc


def _community_event_files(version):
    """Saison-Artikel und Helden-Trial in settings2.bin."""
    parts = _version_parts(version)
    if parts is None:
        return None
    group, trial, _crc = parts
    return {SETTINGS2: settings2(group, trial["Id"] if trial else None)}


def packages(today, forced_event=None):
    group = seasonal_event(today, forced_event)
    version = DATA_VERSION if group == "OrdinaryDay" else DATA_VERSION + "." + group
    trial = free_trials.current(today)
    if trial:
        version += "." + _trial_version(trial)
    if balance.client_version():
        version += "." + balance.client_version()
    # beide Pakete mit derselben Version: unterschiedliche Datenstände
    # lassen den Launcher reparieren (und an der getauschten Exe abstürzen)
    pkgs = [Package(DATA_BIN, "", version, _data_bin_files),
            Package(COMMUNITY_EVENT_SETTINGS, "", version, _community_event_files)]
    if CLIENT_EXE:
        pkgs.append(Package(GAME_BIN, "", _game_version(), _game_bin_files, replace=True,
                            any_installed=True))  # auch ältere --client-exe
    pkgs = pkgs + [_crc_package(p) for p in pkgs]
    # UI-Patches (ohne Prüfsummenliste: GameUI prüft der Launcher nicht)
    return pkgs + [Package(GAME_UI, "", _ui_version(), _game_ui_files, replace=True)]


def T(name):
    return f"DistributionService.Contracts.{name}, DistributionService.Contracts"


def _version(pkg, machine):
    return {"$type": T("RMPackageVersion"),
            "RMPackageId": {"$type": T("RMPackageId"), "DisplayName": "",
                            "MachineType": machine, "PackageName": pkg.name,
                            "RMPackageType": pkg.rm_type},
            "VersionName": pkg.version}


def installed_versions(client):
    """{(RMPackageType, PackageName): VersionName} aus dem
    RMClientPackagesVersion des Launchers (LauncherData/InstalledPackages.json)."""
    out = {}
    for v in (client or {}).get("RMPackageVersions") or []:
        pid = v.get("RMPackageId") or {}
        out[(pid.get("RMPackageType"), pid.get("PackageName") or "")] = v.get("VersionName")
    return out


def launcher_patch():
    """RMLauncherPatch: der Launcher ist aktuell (GetRMLauncherVersion)."""
    return {"$type": T("RMLauncherPatch"), "FullDownloadSize": 0,
            "FullInstallUrl": "", "PatchDownloadSize": 0, "PatchInstallUrl": "",
            "RMLauncherPatchFlags": UP_TO_DATE,
            "VersionName": LAUNCHER_VERSION}


def server_packages(client, base_url, today, machine: int | str = 1, forced_event=None):
    """RMServerPackagesVersion (GetRMPackagesVersion)."""
    return launcher_and_packages(client, base_url, today, machine,
                                 forced_event)["RMServerPackagesVersion"]


def launcher_and_packages(client, base_url, today, machine: int | str = 1, forced_event=None):
    """RMLauncherAndServerPackagesVersion: Launcher aktuell; je Paket
    aktuell, nur Versionsnummer eintragen (Dateien schon richtig) oder
    Patch (Delta zur installierten Version)."""
    have = installed_versions(client)
    patches = []
    for pkg in packages(today, forced_event):
        installed = have.get(pkg.key)
        if installed is None or pkg.installed_files(installed) is None:
            continue  # nicht installiert oder unbekannter Stand: nichts tun
        patch = {"$type": T("RMPackagePatch"), "FullDownloadSize": 0,
                 "FullInstallUrl": "", "PatchDownloadSize": 0, "PatchInstallUrl": "",
                 "RMPackagePatchFlags": UP_TO_DATE,
                 "RMPackageVersion": _version(pkg, machine)}
        if installed != pkg.version:
            if pkg.installed_files(installed) == pkg.files(pkg.version):
                patch["RMPackagePatchFlags"] = UPDATE_VERSION_ONLY
            else:
                patch.update(RMPackagePatchFlags=CAN_INSTALL_PATCH,
                             PatchDownloadSize=len(pkg.patch(installed)),
                             PatchInstallUrl=f"{base_url}/files/{pkg.patch_name(installed)}")
        patches.append(patch)
    return {"$type": T("RMLauncherAndServerPackagesVersion"),
            "RMLauncherPatch": launcher_patch(),
            "RMServerPackagesVersion": {"$type": T("RMServerPackagesVersion"),
                                        "BranchName": "", "GamePublicationLabel":
                                        PUBLICATION_LABEL, "RMPackagePatches": patches}}


def archive_by_name(file_name, today, forced_event=None):
    """Patch-Archiv <Typ><Name>_<von>_<nach>.7z."""
    for pkg in packages(today, forced_event):
        prefix = f"{pkg.rm_type:d}{pkg.name}_"
        suffix = f"_{pkg.version}.7z"
        if file_name.startswith(prefix) and file_name.endswith(suffix):
            installed = file_name[len(prefix):-len(suffix)]
            if pkg.installed_files(installed) is not None:
                return pkg.patch(installed)
    return None


def login_page(name=None, news=""):
    """Anmeldeseite im iframe des Launchers: Name + Passwort (oder
    Registrierung) gegen /launcher/login bzw. /launcher/register (auth.py);
    das Sitzungs-Token kommt in Cookie t, das der Launcher dem Spiel als
    -token mitgibt. Der Launcher erkennt die Anmeldung an Cookie t, aber nur
    beim Laden der Seite (_detectLogin in _updateLocalContent) -> neu laden;
    dazu muss window.userIsLoggedIn der Seite wahr sein.

    Spielerwechsel ohne Abmelden: Nach einem "abgemeldet" sperrt der
    Launcher den Play-Knopf beim nächsten Anmelden (launcher.js
    _detectLogin, addClass('disabled')) und gibt ihn erst wieder frei, wenn
    sein Startablauf durchläuft (showLaunchButton) -- der läuft nur einmal.
    Ist schon jemand angemeldet, meldet die Seite den neuen Spieler daher
    wie das Original beim Neu-Anmelden (autoSignInUserAgainToRedeem...)
    direkt per _onUserLoggedIn; der Launcher startet das Spiel mit dem
    zuletzt gemeldeten Token.

    news: HTML der News (news.News.html) im Kasten unter der Anmeldung,
    über dem Play-Knopf des Launchers; ein Eintrag zur Zeit, mit Pfeilen
    durchzublättern (Karussell).

    name: Spieler zum Cookie t laut Server (None = kein/ungültiges Token,
    z. B. abgemeldet oder gebannt). Die Seite löscht ein ungültiges Cookie
    sofort, bevor der Launcher sie auswertet, sonst bliebe Play aktiv.
    Nur eine Anmeldung zur Zeit (busy): der Launcher-Browser löste das
    Formular doppelt aus, die zweite Anmeldung machte das Token der ersten
    ungültig, und die Seite behielt womöglich das tote.
    Jeder Anmeldeversuch meldet die bisherige Anmeldung zuerst ab
    (_onUserLoggedOut), damit Play nie für den alten Spieler aktiv bleibt."""
    return (LOGIN_PAGE.replace("@VALID@", "true" if name else "false")
            .replace("@NEWS@", NEWS_BOX % news if news else "")
            .replace("@TEXTS@", json.dumps(LOGIN_TEXTS, ensure_ascii=False)))


# Sprachen des Spiels (GameplaySettings/Oasis/oasis_<code>.json) mit
# Eigennamen; Cookie launcher_locale liest der Launcher beim Laden der Seite
# (_detectLocale -> _onUserSelectLanguage). Seine eigene Oberfläche kennt
# nur einen Teil davon und fällt sonst auf Englisch zurück.
LANGUAGES = [("en", "English"), ("de", "Deutsch"), ("fr", "Français"),
             ("es", "Español"), ("es-mx", "Español (México)"),
             ("it", "Italiano"), ("pl", "Polski"), ("pt", "Português"),
             ("pt-br", "Português (Brasil)"), ("ru", "Русский")]

_TEXT_KEYS = ("language", "name", "password", "login", "register", "logout",
              "logged_in", "press_play", "missing", "unreachable",
              "name_invalid", "name_taken", "bad_login", "password_short",
              "banned", "suspended", "too_many_attempts", "maintenance",
              "steam_hint", "steam_new", "steam_expired", "steam_taken", "steam_other")
_TEXTS = {
    "en": ("Language", "Name", "Password", "Log in", "Register", "Log out",
           "Logged in as {0}.", "Press “{0}” to start the game.",
           "Enter name and password.", "Server not reachable.",
           "Name: 3-24 characters, only letters, digits, _ . -",
           "This name is already taken.", "Wrong name or password.",
           "Password too short (at least 6 characters).",
           "This account is banned.", "This account is suspended until {0}.",
           "Too many failed attempts, please wait a minute.",
           "Server maintenance, please try again later.",
           ("Steam account recognised. New here? Play with your Steam account. "
            "Already have an account? Log in below once to link it."),
           "Play as new Steam player", "Steam check expired, please restart the launcher.",
           "This Steam account is already linked to another account.",
           "This account is already linked to another Steam account."),
    "de": ("Sprache", "Name", "Passwort", "Anmelden", "Registrieren", "Abmelden",
           "Angemeldet als {0}.", "Drücke „{0}“, um das Spiel zu starten.",
           "Name und Passwort eingeben.", "Server nicht erreichbar.",
           "Name: 3-24 Zeichen, nur Buchstaben, Ziffern, _ . -",
           "Der Name ist schon vergeben.", "Name oder Passwort falsch.",
           "Passwort zu kurz (mindestens 6 Zeichen).",
           "Dieser Account ist gebannt.", "Dieser Account ist gesperrt bis {0}.",
           "Zu viele Fehlversuche, bitte eine Minute warten.",
           "Serverwartung, bitte später erneut versuchen.",
           ("Steam-Konto erkannt. Neu hier? Spiele mit deinem Steam-Konto. "
            "Schon einen Account? Melde dich unten einmal an, um ihn zu verknüpfen."),
           "Als neuer Steam-Spieler spielen", "Steam-Prüfung abgelaufen, bitte den Launcher neu starten.",
           "Dieses Steam-Konto ist schon mit einem anderen Account verknüpft.",
           "Dieser Account ist schon mit einem anderen Steam-Konto verknüpft."),
    "fr": ("Langue", "Nom", "Mot de passe", "Connexion", "Créer un compte", "Déconnexion",
           "Connecté en tant que {0}.", "Clique sur « {0} » pour lancer le jeu.",
           "Saisis le nom et le mot de passe.", "Serveur injoignable.",
           "Nom : 3 à 24 caractères, uniquement lettres, chiffres, _ . -",
           "Ce nom est déjà pris.", "Nom ou mot de passe incorrect.",
           "Mot de passe trop court (6 caractères minimum).",
           "Ce compte est banni.", "Ce compte est suspendu jusqu'au {0}.",
           "Trop de tentatives échouées, patiente une minute.",
           "Maintenance du serveur, réessaie plus tard."),
    "es": ("Idioma", "Nombre", "Contraseña", "Iniciar sesión", "Registrarse", "Cerrar sesión",
           "Sesión iniciada como {0}.", "Pulsa «{0}» para iniciar el juego.",
           "Introduce nombre y contraseña.", "No se puede conectar con el servidor.",
           "Nombre: 3-24 caracteres, solo letras, cifras, _ . -",
           "Ese nombre ya está en uso.", "Nombre o contraseña incorrectos.",
           "Contraseña demasiado corta (mínimo 6 caracteres).",
           "Esta cuenta está bloqueada.", "Esta cuenta está suspendida hasta {0}.",
           "Demasiados intentos fallidos, espera un minuto.",
           "Mantenimiento del servidor, inténtalo más tarde."),
    "it": ("Lingua", "Nome", "Password", "Accedi", "Registrati", "Esci",
           "Accesso effettuato come {0}.", "Premi «{0}» per avviare il gioco.",
           "Inserisci nome e password.", "Server non raggiungibile.",
           "Nome: 3-24 caratteri, solo lettere, cifre, _ . -",
           "Questo nome è già in uso.", "Nome o password errati.",
           "Password troppo corta (almeno 6 caratteri).",
           "Questo account è bannato.", "Questo account è sospeso fino al {0}.",
           "Troppi tentativi falliti, attendi un minuto.",
           "Manutenzione del server, riprova più tardi."),
    "pl": ("Język", "Nazwa", "Hasło", "Zaloguj", "Zarejestruj", "Wyloguj",
           "Zalogowano jako {0}.", "Naciśnij „{0}”, aby uruchomić grę.",
           "Podaj nazwę i hasło.", "Serwer jest niedostępny.",
           "Nazwa: 3-24 znaki, tylko litery, cyfry, _ . -",
           "Ta nazwa jest już zajęta.", "Błędna nazwa lub hasło.",
           "Hasło jest za krótkie (co najmniej 6 znaków).",
           "To konto jest zbanowane.", "To konto jest zawieszone do {0}.",
           "Zbyt wiele nieudanych prób, odczekaj minutę.",
           "Przerwa techniczna serwera, spróbuj ponownie później."),
    "pt": ("Idioma", "Nome", "Palavra-passe", "Entrar", "Registar", "Sair",
           "Sessão iniciada como {0}.", "Carrega em «{0}» para iniciar o jogo.",
           "Introduz nome e palavra-passe.", "Servidor inacessível.",
           "Nome: 3-24 caracteres, apenas letras, algarismos, _ . -",
           "Este nome já está a ser usado.", "Nome ou palavra-passe errados.",
           "Palavra-passe demasiado curta (mínimo 6 caracteres).",
           "Esta conta está banida.", "Esta conta está suspensa até {0}.",
           "Demasiadas tentativas falhadas, aguarda um minuto.",
           "Manutenção do servidor, tenta novamente mais tarde."),
    "pt-br": ("Idioma", "Nome", "Senha", "Entrar", "Cadastrar", "Sair",
              "Conectado como {0}.", "Clique em “{0}” para iniciar o jogo.",
              "Digite nome e senha.", "Servidor inacessível.",
              "Nome: 3-24 caracteres, apenas letras, números, _ . -",
              "Este nome já está em uso.", "Nome ou senha incorretos.",
              "Senha muito curta (mínimo de 6 caracteres).",
              "Esta conta está banida.", "Esta conta está suspensa até {0}.",
              "Muitas tentativas falhas, aguarde um minuto.",
              "Manutenção do servidor, tente novamente mais tarde."),
    "ru": ("Язык", "Имя", "Пароль", "Войти", "Регистрация", "Выйти",
           "Вы вошли как {0}.", "Нажмите «{0}», чтобы запустить игру.",
           "Введите имя и пароль.", "Сервер недоступен.",
           "Имя: 3–24 символа, только буквы, цифры, _ . -",
           "Это имя уже занято.", "Неверное имя или пароль.",
           "Пароль слишком короткий (не менее 6 символов).",
           "Эта учётная запись заблокирована.", "Эта учётная запись заблокирована до {0}.",
           "Слишком много неудачных попыток, подождите минуту.",
           "Техническое обслуживание сервера, попробуйте позже."),
}
_TEXTS["es-mx"] = _TEXTS["es"]
# Beschriftung des Play-Knopfs, wie der Launcher sie übersetzt
# (LocalLauncher/javascripts/translations, Schlüssel 54); fehlt sie, zeigt
# er Englisch
PLAY_BUTTON = {"de": "Spielen", "fr": "Jouer", "pl": "Graj", "pt": "Jogar", "ru": "Играть"}
LOGIN_TEXTS = {"languages": LANGUAGES, "play": PLAY_BUTTON,
               "texts": {code: dict(zip(_TEXT_KEYS, t)) for code, t in _TEXTS.items()}}

NEWS_BOX = ('<div id="news"><div id="nav"><a onclick="page(-1)">&#8249;</a>'
            '<span id="pg"></span><a onclick="page(1)">&#8250;</a></div>'
            '<div id="items">%s</div></div>')

LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Mighty Quest Login</title>
<style>
html,body{background:transparent}
body{font-family:sans-serif;margin:0;color:#eee}
#col{position:absolute;top:56px;right:48px;width:326px;height:390px;display:flex;flex-direction:column}
#box{flex:none;padding:18px 22px 20px;
background:rgba(18,14,34,.82);border:1px solid rgba(255,255,255,.18);border-radius:8px;
box-shadow:0 4px 18px rgba(0,0,0,.5)}
label{display:block;margin:8px 0 2px;font-size:14px;color:#ccc}
input{font-size:16px;padding:7px;width:262px}
button{font-size:15px;margin:14px 8px 0 0;padding:7px 14px;cursor:pointer}
#who{color:#9f9;margin-top:0} #err{margin-top:12px;color:#f99}
#lang{text-align:right;margin-bottom:4px} #lang select{font-size:13px;padding:3px}
#news{flex:none;margin-top:12px;padding:10px 22px 12px;background:rgba(18,14,34,.82);border:1px solid rgba(255,255,255,.18);
border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.5);font-size:13px}
#discord{float:left;padding:3px 10px;border-radius:4px;
background:#5865f2;color:#fff;font-size:14px;text-decoration:none}
#items{overflow-y:auto}
#items::-webkit-scrollbar{width:8px} #items::-webkit-scrollbar-thumb{background:rgba(255,255,255,.3);border-radius:4px}
#items .n{display:none} #items .n.on{display:block} #news .d{color:#aaa} #news p{margin:3px 0 0;color:#ddd}
#nav{text-align:right;color:#aaa;margin-bottom:4px;-webkit-user-select:none}
#nav a{display:inline-block;width:22px;text-align:center;font-size:20px;line-height:18px;color:#ddd;cursor:pointer;border-radius:4px}
#nav a:hover{background:rgba(255,255,255,.15)} #pg{display:inline-block;min-width:38px;text-align:center}
</style></head><body>
<div id="col"><div id="box">
<div id="lang"><a id="discord" href="https://discord.gg/2c6vSU4NYY" target="_blank">Discord</a><select id="sel" onchange="setLang(this.value)" aria-label="Language"></select></div>
<div id="f">
<label for="name" data-t="name"></label><input id="name" autocomplete="username" onkeydown="enter(event)">
<label for="pw" data-t="password"></label><input id="pw" type="password" autocomplete="current-password" onkeydown="enter(event)">
<div><button type="button" onclick="send('login')" data-t="login"></button><button type="button" onclick="send('register')" data-t="register"></button></div>
<div id="sn" style="display:none"><p id="snt" style="font-size:13px;color:#ccc;margin:12px 0 0" data-t="steam_hint"></p>
<button type="button" onclick="steamNew()" data-t="steam_new" style="margin-top:6px"></button></div>
</div>
<div id="in" style="display:none"><p id="who"></p><p id="play"></p>
<button type="button" onclick="logout()" data-t="logout"></button></div>
<div id="err"></div>
<form id="sf" method="post" action="/launcher/steam" target="sfr" style="display:none"></form>
<iframe name="sfr" style="display:none"></iframe>
</div>
@NEWS@</div>
<script>
var valid=@VALID@, L=@TEXTS@, busy=false;  // busy: keine zweite Anmeldung, solange eine läuft
function set(n,v,days){var d=new Date();d.setTime(d.getTime()+days*864e5);
document.cookie=n+"="+encodeURIComponent(v)+";expires="+d.toGMTString()+";path=/";}
function get(n){var m=document.cookie.match(new RegExp("(?:^|; )"+n+"=([^;]*)"));return m?decodeURIComponent(m[1]):"";}
function lang(){var c=get("launcher_locale")||((location.search.match(/[?&]locale=([^&]*)/)||[])[1]||"").toLowerCase();
return L.texts[c]?c:(L.texts[c.split("-")[0]]?c.split("-")[0]:"en");}
function tr(k,a){return (L.texts[lang()][k]||L.texts.en[k]||k).replace("{0}",a||"");}
function setLang(c){set("launcher_locale",c,365);location.reload();}
function post(what,data,ok,fail){var x=new XMLHttpRequest();x.open("POST","/launcher/"+what,true);
x.setRequestHeader("Content-Type","application/json");
x.onreadystatechange=function(){if(x.readyState!=4)return;
var r=null;try{r=JSON.parse(x.responseText);}catch(e){}
if(x.status==200&&r)ok(r);else if(fail)fail();};
x.send(JSON.stringify(data));}
function err(k,a){document.getElementById("err").textContent=k?tr(k,a):"";fit();}
function send(what){if(get("t")){set("t","",-1);set("hyperquest_launcher_session","",-1);
window.userIsLoggedIn=false;show();try{parent._launcher._onUserLoggedOut("");}catch(e){}}
var n=document.getElementById("name").value.trim(),p=document.getElementById("pw").value;
err();if(!n||!p){err("missing");return;}
if(busy)return;busy=true;
post(what,{name:n,password:p,proof:proof},function(r){busy=false;
if(r.Error){err(r.Error,r.Until?new Date(r.Until).toLocaleString():"");return;}
login(r.Token,r.Name);},function(){busy=false;err("unreachable");});}
// Kein <form>: der Steam-Launcher fängt jedes submit ab und schickt das
// Formular mit steam_ticket selbst ab (launcher.js _addSteamTicketUpdateListeners),
// das lud die Seite mitten in der Anmeldung neu
function enter(e){if((e.keyCode||e.which)==13)send("login");}
// Steam-Anmeldung: ein künstliches submit am versteckten Formular lässt den
// Steam-Launcher sein Ticket anhängen und das Formular in den versteckten
// Iframe schicken (steam_auth.py). Bekannte SteamID: gleich angemeldet.
// Unbekannte: Beweis (proof) merken, "neuer Spieler" anbieten bzw. den
// Account beim Anmelden mit Name + Passwort verknüpfen. Ohne Steam oder ohne
// Ticket bleibt alles still.
var proof="";
function steamResult(r){
if(r.Token){login(r.Token,r.Name);return;}
if(r.Proof){proof=r.Proof;document.getElementById("sn").style.display="";fit();return;}
if(r.Error&&r.Error!="steam_no_ticket"&&r.Error!="steam_disabled")err(r.Error,r.Until?new Date(r.Until).toLocaleString():"");}
function steamTry(){var e=document.createEvent("Event");e.initEvent("submit",true,true);
document.getElementById("sf").dispatchEvent(e);}
function steamNew(){if(busy||!proof)return;busy=true;err();
post("steamnew",{proof:proof},function(r){busy=false;
if(r.Error){err(r.Error);return;}login(r.Token,r.Name);},function(){busy=false;err("unreachable");});}
if(!get("t"))setTimeout(steamTry,800);
function login(t,n){var was=window.userIsLoggedIn;
set("t",t,365);set("n",n,365);set("email",n+"@mqel.local",365);set("hyperquest_launcher_session",t,365);
try{if(was&&parent._launcher){parent._launcher._onUserLoggedIn(JSON.stringify(
{LoginToken:t,SGToken:t,UserEmail:n+"@mqel.local"}));show();return;}}catch(e){}
location.reload();}
function logout(){set("t","",-1);set("hyperquest_launcher_session","",-1);location.reload();}
function show(){var t=get("t");window.userIsLoggedIn=!!t;
document.getElementById("f").style.display=t?"none":"";
document.getElementById("in").style.display=t?"":"none";
document.getElementById("who").textContent=t?tr("logged_in",get("n")):"";
if(window.fit)fit();}
var sel=document.getElementById("sel"),cur=lang();
for(var i=0;i<L.languages.length;i++){var o=document.createElement("option");
o.value=L.languages[i][0];o.textContent=L.languages[i][1];o.selected=o.value==cur;sel.appendChild(o);}
document.documentElement.lang=cur;
var els=document.querySelectorAll("[data-t]");
for(var j=0;j<els.length;j++)els[j].textContent=tr(els[j].getAttribute("data-t"));
document.getElementById("play").textContent=tr("press_play",L.play[cur]||"Play");
if(!valid&&get("t")){set("t","",-1);set("hyperquest_launcher_session","",-1);}
if(!get("launcher_locale"))set("launcher_locale",cur,365);
show();
// News-Karussell: ein Eintrag zur Zeit; der Kasten füllt die Spalte bis
// unten (über dem Play-Knopf), lange Einträge scrollen darin. Höhe per JS,
// weil das Flex-Schrumpfen im Launcher-Chromium (2015) nicht greift
var cur_n=0;
function page(d){var n=document.querySelectorAll("#items .n");if(!n.length)return;
n[cur_n].className="n";cur_n=(cur_n+d+n.length)%n.length;n[cur_n].className="n on";
document.getElementById("pg").textContent=(cur_n+1)+" / "+n.length;
document.getElementById("items").scrollTop=0;}
function fit(){var nb=document.getElementById("news");if(!nb)return;
var col=document.getElementById("col"),box=document.getElementById("box"),items=document.getElementById("items");
var h=col.clientHeight-box.offsetHeight-12-(nb.offsetHeight-items.offsetHeight);
items.style.height=Math.max(h,40)+"px";}
if(document.getElementById("news")){page(0);
if(document.querySelectorAll("#items .n").length<2)document.getElementById("nav").style.display="none";}
fit();
// Play nach Abmelden/Anmelden: der Launcher sperrt den Knopf bei jeder
// Anmeldung und gibt ihn nur einmal frei (showLaunchButton nach dem
// Patch-Check). War er schon frei (gemerkt im Launcher-Fenster, das das
// Neuladen der Seite überlebt), gibt die Seite ihn wieder frei.
function playButton(){try{return parent.$("#launch-button");}catch(e){return null;}}
setInterval(function(){var b=playButton();if(!b)return;
if(b.is(":visible")&&!b.hasClass("disabled"))parent._mqPlayReady=true;
else if(get("t")&&parent._mqPlayReady&&b.is(":visible"))b.removeClass("disabled");},300);
</script>
</body></html>"""


def parse_client_packages(query, body):
    """rmClientPackagesVersion aus JSON-Body oder Query."""
    if isinstance(body, dict) and "rmClientPackagesVersion" in body:
        return body["rmClientPackagesVersion"], body.get("machineType", 1)
    raw = (query.get("rmClientPackagesVersion") or [""])[0]
    try:
        client = json.loads(urllib.parse.unquote(raw)) if raw else None
    except ValueError:
        client = None
    machine = (query.get("machineType") or ["1"])[0]
    return client, int(machine) if machine.isdigit() else machine