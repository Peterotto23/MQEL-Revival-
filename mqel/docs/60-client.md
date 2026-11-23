# Client Directory (`client/`)

Ready-to-play tree, assembled from the release archives. For the status of the
GameData extraction + merge steps see `BUILD.md`. The GameplayToolbox
is deliberately **not** included (server/admin tool, not needed by players).

## Layout

```text
client/
  BUILD.md                 How the tree was built (package → target)
  GameData/                ← 0.36.1.34.0_104956_GameData.7z + …_GameUI.7z
    Bin/                   Engine binaries, CEF, Boot.tsc, gear.ini
      MightyQuest.exe            ← from the Game package (packed ship binary)
      MightyQuest_release.exe    ← unpacked, for analysis/debugging
      MightyQuest_profile.exe    ← unpacked (profiling build)
      MightyQuestWorker*.exe     ← simulation worker (3 variants)
      mightyquest-ui.exe         CEF render process (lobby/shop UI)
      steam_api.dll, libcef.dll, … 
    Config/Game/*.json     Engine/user settings (among others UserSettings.json)
    Data/GameplaySettings/ ~8700 rule JSONs (items, spells, shop, …)
    Data/Scripts/*.tsc     Boot/init scripts (boot via Bin/Boot.tsc)
    Data/Generated/        generated assets
    Data/UI/               ← from the GameUI package (lobby images, HTML)
    Bigfiles/PC            Bigfile packages (loaded via Boot.tsc)
    version.GameData.txt   Package ID "104956"
    version.GameUI.txt     Package ID "280139"
    version.Game.txt       Package ID "280139" (copied along from the Game package)
  Launcher/                ← 0.36.1.34.0_276072_PublicLauncher.7z
    PublicLauncher.exe     Qt launcher (patch check, login page, game start)
    PublicLauncherSettings.json   ← generated from the template (local URLs, see below)
    PublicLauncherSettings.Template.json (original template for reference)
    LocalLauncher/         Launcher web UI (HTML/JS/CSS)
    Installer/Prerequisites/ DirectX/VC redists (for player setup)
    LauncherPatcher.exe, MQELDiagnostics.exe, 7z.exe, …
    version.PublicLauncher.txt   Package ID "276072"
  LauncherData/            Launcher runtime data (partly generated)
    InstalledPackages.json Package state (reconstruction, format TODO)
  Temp/_Launcher/
    LauncherPatcher.exe    Copy (the launcher expects it at
                           ..\Temp\_Launcher\LauncherPatcher.exe)
```

## Ways to start

1. **Direct start (revival/debug route):** CWD = `GameData/Bin`,
   `MightyQuest_release.exe -server_url http://127.0.0.1:PORT -token <t>
   -email <mail> -environmentName local -httpCompression 0`
   (further args: `-steamid/-steamticket` only for Steam,
   `-gameLanguage`, `-attackInputMode`, …). The launcher is bypassed.
2. **Via launcher (player route, later):** `Launcher/PublicLauncher.exe`
   with `PublicLauncherSettings.json`:
   `LauncherWebSiteUrl` (login page, own hosting),
   `PatcherServiceUrl` (version check — must report "up to date"),
   `GameServerUrl` (our server), `MaintenanceModeVerificationUrl`.
   The launcher starts `..\GameData\Bin\MightyQuest.exe`.

## Launcher and patcher (server/patcher.py)

`PublicLauncherSettings.json` points at the game server:
`LauncherWebSiteUrl` = `/launcher/` (login page, sets cookie `t` = token),
`PatcherServiceUrl` = `/patcher`, `MaintenanceModeVerificationUrl` =
`/launcher/maintenance` (empty = no maintenance check). At startup the
launcher fetches the URL via GET (`TaskCheckMaintenance`, response handler 0x4d6813):
status 200 = normal, any other status shows the local
`LocalLauncher/launcher-maintenance.html`; the body is not read
(this path does not use `ProxyMaintenanceInfoModel` with StartTime/EndTime).
In maintenance mode (web tool, `admin.SetMaintenance`,
stored in `state/<profile>/maintenance.json`) the server responds with 503. `LauncherData/InstalledPackages.json` is an
`RMClientPackagesVersion` (enums as numbers); the Steam installation lists,
among others, GameBin (1) 280139, DataBin (2) 104956, DataBF (4) per bigfile.
The launcher discards an unknown format.

The server delivers **no game data**, only xdelta deltas (VCDIFF) against the
files of the Steam installation. Package DataBin (version `104956` = Steam,
`104956.<group>` = event) changes the group `OrdinaryDay` in `Bin/Boot.tsc` to the seasonal
event by date (`patcher.EVENTS`, Easter is movable); force it with
`game_server.py --seasonal-event Halloween`. The launcher then starts
`GameData\Bin\MightyQuest.exe` with `-token/-email/-server_url`.
Confirmed in testing (plaintext tree, without Steam mode): the launcher downloads
`2_104956_104956.Halloween.7z` without asking, applies it, and the game
shows the Halloween decorations; after the event ends the server offers the way back
`104956.Halloween → 104956`. In Steam mode the launcher refuses
patches ("Please update via the Steam client").

Package CommunityEventSettings (10, same version names) appends to
`Data/GameplaySettings/settings2.bin` a `SkuCommunityEvent` (Id 9000) with
the event's inactive catalog SKUs (`patcher.EVENT_SKU_KEYWORDS`); the
client unlocks SKUs from SkuCommunityEvents within the time window (that is how
the Daily Deals worked too). The client computes times in 32 bit, so the end must be before
2038. Round trip of both packages in the launcher confirmed (2026-10-15:
`104956 → 104956.Halloween → 104956`, decorations and shop items come and
go). Deltas only fit the reported state: anyone who edits files or
`InstalledPackages.json` by hand can make the package state and the files
diverge; after a failed patch the launcher deletes the
package entry, and then the server no longer patches the package (add the entry back by hand with the
actual state). Confirmed for Halloween: creature variants in the lab and the
four barricades in the shop. Remaining invisible: "Knight Halloween Batwings"
(ShopContext 6) and `Halloween_Theme_2013` (`PrivateTheme`) — leftovers from 2013;
the Halloween Pack Oct2014 (Pack 67) contains exactly creatures, barricades and theme 6.

**Game exe and checksums:** after a patch the launcher (`TaskRepair`) checks
every file against `..\GameData\crc\{GameBin,DataBin,
CommunityEventSettings}CRC.json` (CRC32 + size). With the
hand-swapped `MightyQuest.exe` ("CRC mismatch!") it finds no package and
crashes (null pointer 0x4d91a1); a second start went through. That is why the
server distributes the exe itself: `game_server.py --client-exe <exe>`
delivers package GameBin (1, version `280139.X<CRC32>`) as
`MightyQuest.exe.replace` (the launcher deletes the old file and renames,
0x4e2d72; so it fits any installed exe). Every changed package
comes with its checksum list containing the new values (GameCRC 9, DataCRC 7,
CommunityEventSettingsCRC 11, same version, also `.replace`). The
lists are in no archive; the templates are those of a Steam installation
in `server/data/crc/`. The distributed exe is built by
`tools/patch_ca.py MightyQuest_profile.exe <root.pem> --out
MightyQuest_player.exe --no-fps` (own CA, FPS display off). The
UI patches (`server/ui_patches.py`) come as package GameUI (8, version
`280139.U<CRC32>`, whole files via `.replace`); GameUI has no
checksum list. `tools/patch_ui.py` applies the same patches locally.

**Player setup from the Steam installation** (confirmed 2026-10-17):
have Steam verify/repair the installation (this also resets
`LauncherData/InstalledPackages.json` to the Steam state, the depot
contains it), replace `Launcher/PublicLauncherSettings.json` with the version with
our URLs, and run `website/steam-launcher-patch.cmd` in the
installation folder. Then start the game from Steam: the
patcher delivers the exe, UI patches, data and checksum lists, and the game
starts. For HTTPS with a self-signed certificate, additionally do the
`certutil` import (see the HTTPS paragraph below).

**Steam mode** (`SteamAPI_Init` succeeds, flag `[Launcher+0x61]`, e.g.
with `steam_appid.txt` or when started from Steam): when building
the "version old" state machine (game, launcher, repair), the launcher wires it directly
to `TaskExit` (`jne` at VA 0x4e10a7/0x4e1135/0x4e1225) and shows the
dialog 0x4def05 beforehand ("Please update via the Steam client"). `steam-launcher-patch.cmd`
(batch/PowerShell hybrid, patches the player's exe, no Ubisoft file
on the website) turns the three `jne` into `nop` and 0x4def05 into `ret`;
since then the launcher patches and starts from Steam as well (tested 2026-10-19).
**Steam login** (`server/steam_auth.py`, environment variable
`MQEL_STEAM_API_KEY`, Steam Web API key): the login page has a hidden
`<form>` (target: hidden iframe, `POST /launcher/steam`) and after 0.8 s triggers
an artificial `submit`; the Steam launcher intercepts it, appends
`steam_ticket` (hex, `GetAuthSessionTicket`) and sends it off (without Steam
the ticket stays empty, the page stays silent). The server verifies the ticket at
`ISteamUserAuth/AuthenticateUserTicket` (appid 239220). Known SteamID → logged
in immediately. Unknown → proof (`proof`, 10 min, in memory only): "Play as a new
Steam player" creates an account without a password (name from the Steam profile,
`logins.json`: `SteamId` without `Hash`), or a legacy player logs in once with
name + password and thereby links their account. An account has at most
one SteamID and vice versa. Confirmed in testing: the ticket arrives, Steam returns the
SteamID.

Steam achievements are set by the game exe itself (`SteamUserStats`
SetAchievement/StoreStats, `SteamId` in `AchievementSettings.json`): live on
`AchievementCompleted` and by reconciling the already completed ones; in testing
all 13 were granted retroactively on the first fight.

`server/settings_archive.py` reads/writes the archive format of
`settings.bin`/`settings2.bin`: encrypted (keystream 0x116ae60)
LZO1X + u32 size, inside it a folder tree of JSON files with CRC32 at the end.
Changes at the end of the file leave the beginning byte-for-byte identical (small
xdelta).

Original ship config (Steam): `LauncherWebSiteUrl`
`https://launcher.themightyquest.com/mqel-live/launcher/load/`, `GameServerUrl`
`https://gs.themightyquest.com/mqel-live.gameserver`, `PatcherServiceUrl`
`https://gs.themightyquest.com/mqel-live.distribution`,
`MaintenanceModeVerificationUrl` `https://launcher.themightyquest.com/mqel-live/launcher/`,
`EnvironmentName` `mqel-live`, `HttpCompression` `true`, window 822×588,
View 9/12 787×552, `TwitterFeedName` `Sir_Painhammer`.

**The ship exe needs https:** the final `MightyQuest.exe` compares the
server URL with `"https"` and otherwise runs the script `Exit` (from the
memory dump: ship image base 0x460000, check at 0x7e9b4a,
`strncmp(url, "https", 5)`). The server therefore also serves HTTPS with
`--tls-port 8443` (self-signed certificate in
`server/state/tls/`). That doesn't help: the exe only trusts embedded
CAs (DigiCert High Assurance EV Root, COMODO RSA, AddTrust) and reads
neither `SSL_CERT_FILE` (the OpenSSL default paths are never loaded) nor a
CA file for curl. Practically nobody issues certificates under these roots anymore. Without
tampering with the (packed) exe it therefore doesn't run; local
testing uses `MightyQuest_profile.exe` as `MightyQuest.exe` and
`GameServerUrl` = `http://…:8081`.

**HTTPS with `MightyQuest_profile.exe`** (tested 2026-10-17): for `https` URLs the exe
(libcurl + OpenSSL 1.0.1e, TLS 1.2) sets
`SSL_VERIFYPEER`/`VERIFYHOST` (0xfe0f89) and loads its four PEM roots
unpacked from `.rdata` (0xfe145d ff.). `tools/patch_ca.py` replaces one
of them (default: AddTrust) with your own certificate, e.g.
`server/state/tls/cert.pem` of the game server (`--tls-port 8443`, valid only
for its IPs). With `GameServerUrl` = `https://…:8443` the game
then runs encrypted. The chat (5222) stays plaintext (no STARTTLS).
The launcher and Bling shop (CEF, Chromium July 2015) speak TLS 1.0/1.1
(the server allows it, ECDHE only) and verify against the
Windows certificate store: a self-signed certificate must be
imported there (`certutil -addstore -user Root cert.pem`); with a
domain and Let's Encrypt this is unnecessary (still untested).

## Known pitfalls

- `Data/UI/Js` + `Data/UI/scripts` from `0.36.1.34.0_280139_GameUI.7z`
  must be extracted too, otherwise the lobby loads forever (272× 404 in
  `static.log`). The client serves `http://localhost/gamedata/` itself
  (CEF handler `CEFClient_Z.cpp` → `../Data`); a web server on port 80
  is not needed (the former `server/static_server.py` was removed).
- Data (GameData 104956) and exe (branch Update3) don't match exactly
  (`Unhandled member` warnings, among others `Spec.IsVisible`,
  `GeneralSettings.*`).
- **Our client is the final released version**: byte-identical to the
  final Steam depot, built one day before it went live (compared
  by the project owner, 2026-10-16). The server DLLs from the
  GameplayToolbox archive are almost 17 months older (the toolbox's copy,
  not the live server). Evidenced by build numbers (file names of the
  archives) and the most recent file dates inside them: client `280139` (Game/GameUI,
  2015-07-21), launcher `276072` (2015-05-22), toolbox `main.348.1_232184`
  (2014-02-27); GameData `104956` counts independently (2015-07-17); the server was developed further afterwards, and the
  final client knows fields that are missing there (`MineDestroyedTimeBonusCount`,
  `TrophyScoreRestart*`, `LoseTrophyCooldownTimer`,
  `CastleInfoSummary.TrophyCooldownTimer`). Consequence: game rules and
  formulas come from the server code (dnSpy export, local in `decompiled/`); in
  case of discrepancies the client always wins (field readers in the binary,
  `tools/client_members.py -t`), not the contracts.

## Open points (runtime-generated / to be verified)

- `Config/Synergy/Game.ini` (`ReadGameConfigFile ..\Config\Synergy\Game.ini`
  in `Init_Game.tsc`) is in no package — presumably generated or
  optional. A dynamic test will show whether/what is missing.
- `GameData/Config/Game/{CustomUserSettings,EngineSettings}.json`
  (+ `.bak`) are created at runtime.
- `LauncherData/Background.png` (loaded via `BackgroundImageUrl`),
  `LauncherData/Awesomium/` (browser cache) are created at runtime.
- The window values in `PublicLauncherSettings.json` are estimates
  (template placeholders); correct them on the running launcher if needed.
- Player setup additionally needs: DirectX (Installer/Prerequisites),
  VC++ 2010/2012 x86, possibly the Steam client for Steam tickets.