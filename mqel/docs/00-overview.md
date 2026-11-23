# MQEL Revival — Overview

**Goal:** Build a compatible game server for *The Mighty Quest for Epic Loot*
(Ubisoft, 2014–2015, Opal engine, PC) so that the game becomes playable again
with the client files at hand. A 1:1 restoration is impossible (no server-side
data/DBs are known); the aim is a clean rebuild of the **client↔server
protocol** with newly implemented game logic.

**Key findings (as of 2026-10-13):**

- `main.348.1_232184_GameplayToolbox.7z` contains **no runnable server**,
  but something almost as valuable: the complete **.NET server assemblies**
  (`HyperQuest.GameServer.dll`, `*.Contracts.dll`, `Util.*.dll`) including
  XML documentation and PDBs. The **entire server API** (services, methods,
  parameters, DTOs) can be reconstructed from them.
- The client↔server protocol is **HTTP + JSON**, home-built (the "Argo"
  service framework, not WCF/standard REST). URL scheme, headers, and
  auth and anti-cheat HMAC have been reconstructed statically
  (details: `30-protokoll.md`, `40-auth.md`).
- The game client consists of two processes: `MightyQuest.exe` (packed,
  contains the network/HTTP code) and `MightyQuestWorker.exe` (unpacked,
  simulation/replay, talks to the master via a named pipe).
- The server settings (`GameplaySettings/*.json`, ~8700 files) are in
  `0.36.1.34.0_104956_GameData.7z` and can be read by the rebuild **directly
  from disk** — exactly like the original (`GameplaySettingsPath`).

**Doc structure:**

| File | Contents |
|---|---|
| `10-quellen.md` | Inventory of the 5 archives, what is in each |
| `20-architektur.md` | Processes, launcher flow, server components, dependencies |
| `30-protokoll.md` | HTTP+JSON protocol (URL scheme, verbs, headers, envelopes, versioning) |
| `31-services.md` | Service catalog (which services the game really needs) |
| `40-auth.md` | Login flow, ConnectionToken, `[Authenticated]`, client HMAC (Murmur3) |
| `50-revival-plan.md` | Build plan: what to implement, in what order |
| `analysis/api-gameserver.txt` | Complete API map (service → methods, parameters, verbs) |
| `analysis/api-common.txt` | API map of the shared contracts (Tracking, CrashReport, …) |

**Tools** live in `../tools/` (see `../tools/README.md`):
`ildump.py` (IL disassembler), `dnbrowse.py` (type/string browser),
`cadecode.py` (custom attribute decoder), `apimap.py` (API map generator),
`callfinder.py` (caller search).