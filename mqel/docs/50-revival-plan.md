# Revival Build Plan

## A. Already clarified statically ✅

- Archive inventory; no runnable server in `main.*.7z`, but **complete
  server assemblies + API docs**.
- Process model (launcher → master (packed) ↔ worker (pipe) → HTTP).
- **Protocol**: `GET/POST {GameServerUrl}/{Service}.hqs/{Method}[?params]`,
  JSON body `{"param":…}`, headers `t/i/h/rnd/n/g`, errors as JSON with
  HTTP status, `BodyStyle=bare` (no wrapper).
- **Auth**: ConnectionToken via header `t`; client HMAC = Murmur3 + secret
  (algorithm known exactly; run with it disabled at first).
- **API map**: 25+5 services, ~220 endpoints with parameter names
  (`docs/analysis/*.txt`, tool `tools/apimap.py`).

## B. Next analysis steps

1. **JSON details** (own doc / verify dynamically):
   property names (C# names 1:1?), enum/DateTime/ObjectId/decimal format,
   `ServiceException` JSON fields, GET conversions, `$type` fields
   (`TypeNameHandling`).
2. **`[Authenticated]` mapping** on `HyperQuest.GameServer.dll`
   (which methods need a token?) + `EnableClientHmac` default.
3. **Login implementations** (`SleepyGiantService.Login/ValidateToken`,
   `ConnectionManager`): exact session setup, `LoginResult` contents.
4. **Capture the start flow** (dynamic, see C): real request order
   after game start, real JSON samples.
5. **GameplaySettings inventory**: which `*.json` files the server loads
   (`RootSettingsPath`, `[JsonPath]` classes), news/shop paths.
6. **GameCommand model** (`SendCommands`, `EndAttackParams`,
   replay format) for attack resolution.

## C. Dynamic analysis (running since 2026-10-13, current status)

- Logging server: `server/logging_server.py`, today `server/game_server.py`
  (renamed when it became the actual game server; all requests → console +
  `server/requests.log.jsonl`), responses via `server/responses.json`
  (reloaded per request, `$file:` includes for large stubs).
- First capture: `SendCommands` (GameStart tracking) →
  `GetAccountInformation` → client crash on the loading screen (empty `{}`).
- `GetAccountInformation` stub: `server/build_account_stub.py` builds the
  response from `GameplaySettings/AccountTemplates/000003 - FullAccount.json`
  (4 heroes level 30, wallet, castle blueprint, objectives…); shape via
  reflection over the original contracts (`server/gen_stub.ps1`,
  catalog: `docs/analysis/dto-catalog.txt` — 1495 classes, 223 enums).
- AccountTemplates (`GameplaySettings/AccountTemplates/*.json`) are the
  **server-side seed data** for `AdminService.ResetAccount` /
  `CreateFullyFunctionalAccount(…, templateId, …)` (TemplateId =
  file name prefix, e.g. `000001` = DefaultAccount with
  `ShouldResetDisplayName=true` → `ChooseDisplayName` flow).
- Next client run with the filled account stub pending.
- Status of run 2: the client stops after `GetAccountInformation` (no
  further server requests), shows `NETWORK ERROR …`, and on exit
  `OPAL ASSERT - XmppInfo cannot be null … ChatController::LeaveRoom:438`.
  Client strings show its own XMPP implementation (`XmppClient_Z.cpp`,
  `AccountServerController::GetXmppInfo`, `Invalid XmppInfo in
  AccountInformation`, `m_currentAccountInfo->ClientSettings->XmppInfo`).
  Hypothesis: `XmppInfo` is validated right after the account and the
  error dialog comes from there (not from the HTTP traffic).
- Countermeasure: the stub now delivers a plausible `XmppInfo` (`Enabled=true`,
  `Server=127.0.0.1`, `Port=5222`, …); `logging_server.py --tcp-ports 5222`
  uses a honeypot to show whether the client dials XMPP. If so: fake XMPP
  (SASL PLAIN + bind + presence) as the next building block.
- Run 3: `OPAL ASSERT Functional server error 0x80030003` in
  `AccountServerController::OnAccountInformationTaskError:449` (task
  `GetAccountInformationTask`), afterwards ignored: `Invalid
  AccountInformation … GetAccountForgedItem:502` (follow-on error without a
  loaded account), then game UI `NETWORK ERROR`, on exit again
  `LeaveRoom:438`. The only stub change was `XmppInfo` → evidence that the
  account task includes the XMPP connect and fails without a chat server.
- Built: `server/xmpp_server.py` (fake XMPP: stream/PLAIN/bind/session,
  empty roster, MUC self-join, keepalive; everything → `server/xmpp.log`;
  smoke test with the complete handshake passed). Client expectations
  confirmed from binary strings (`Did not receive stream features after SASL
  authentication.`, `Bind successful.`, `STARTTLS not supported.` →
  plaintext without a TLS offer is intended).
  Note: `xmpp_server.py` exits cleanly with Ctrl+C (timeout loop +
  signal handler, socket is closed); if a terminal hangs,
  as a last resort end the `python.exe` with `xmpp_server` in its command
  line via Task Manager/`Stop-Process`.
- Incidentally: `Inventory.ForgedItem` is non-null (`ForgeMode=None`),
  otherwise `GetAccountForgedItem:502` asserts.
- Run 4 (stub with `Enabled=true`, everything 127.0.0.1): `0x80030003` again,
  **no** XMPP contact, no further HTTP requests. Client logs
  (`Bin/MQLog.txt`, `Bin/NetworkLog.Txt`) show: the HTTP response arrives
  completely (`ARGO: Request 1 completed`), the assert fires ~50 ms
  later — so a **local** failure in the account processing, not a
  network timeout. Shutdown sequence logged cleanly
  (`ClientIdleCommand` + `GameStateTracking` + `LogoutCommand`, i=2).
- Next hypothesis: the client connects to `Domain`/`ConferenceServer`
  instead of `Server` (DNS failure without TCP) → the stub now uses
  `127.0.0.1` everywhere (JIDs `test@127.0.0.1/…`, fake XMPP mirrors any domain).
- Independently confirmed: the lobby UI loads from `http://localhost/gamedata/…`
  (`WebviewManager::Initialize() - lobbyPageUrl`, see MQLog) → own
  `server/static_server.py` (`/gamedata/` → `GameData/Data/`,
  `/AG_BA073_01.gamewebsite/` → `GameData/`, 404 logging; port 80 =
  admin terminal needed). Explains the game UI `NETWORK ERROR` from run 2.
  *Later disproved (Oct. 2026):* the client serves `/gamedata/` itself
  (CEF handler → `../Data`), the game runs without the static server; it
  was removed.
- Experiment: `XmppInfo.Enabled` back to `false` (the only variable),
  the rest (lobby with the :80 server) unchanged — goal: reach the lobby, chat
  later (see comment in `build_account_stub.py`).
- Run 5 (screenshot): `0x80030003` despite `Enabled=false` → `Enabled` was
  never the variable. Remaining diff to run 2 (task OK): filled
  XmppInfo fields + `ForgedItem` null→object. Diagnostic variants
  `server/stubs/ai-A.json` (only Xmpp empty) / `ai-B.json` (exact
  run-2 state), switchable via `responses.json` while the server is running
  + assert dialog button "Retry" (`server/make_variants.py`).
  Result: error in BOTH cases → neither the Xmpp fields nor ForgedItem.
- New hypothesis: castle integrity (`AccountPrivilege.ByPassCastleIntegrityCheck`
  exists; our draft had 3 EMPTY rooms). `010030 - FullAccount_30_FullUpdate.json`
  contains the only COMPLETE castle (rooms with buildings/creatures) →
  `build_account_stub.py` now uses it for the draft (4 rooms, 9 buildings,
  1 creature; DTO shapes `CastleBuilding/CastleCreature/Orientation` from
  dto-catalog). Base for the rest = 010030 (priv 232), objectives still from 000003.
- Result: SAME error, "Retry" crashes the client (retry route dead). Change
  of strategy: **structure vs. content** — `server/stubs/ai-S.json`
  (reflection skeleton: all 37 keys, empty defaults, 2.7 KB) as a new
  experiment; build up additively on success. At the same time, transport
  true to the spec: `logging_server.py` speaks HTTP/1.1 + keep-alive and sends
  the `sl` header (verified). **Note: server restart required**
  (code change, not a plain responses.json reload).
- Skeleton result: SAME error → content ruled out. RE finding
  (static, `MightyQuest_release.exe` is 32-bit!): task invokers
  (`0x115eca0` ff.) map a sub-call failure → `0x80030003`
  (`0x80030002` = null arg); `OnAccountInformationTaskError` only swallows
  `0x80000001` (cancel). The client JSON parser is strict and hand-written
  (`Expected [true] or [false]`, `Not a number`, `Not a time span`,
  `Expected character/string`, `Max json string exceeded`,
  `Cannot get object serializer [%s]`); DateTime = `"YYYY-MM-DDTHH:MM:SSZ"`
  (Z mandatory), but `""` → MinValue is OK. The error logger is NULL on this
  path (silent). Whether the `sl` header matters: unclear.
- New hypothesis: after the account parse, a SECOND request to a DIFFERENT
  target follows (not GameServer/XMPP/:80 — nothing arrives there), which
  fails immediately. Test: `Get-NetTCPConnection` per
  MightyQuest process during the hang (SYN targets!).
- Netstat result: NO outbound except 2× keep-alive to :8081; instead the game
  listens on 1337/9920/12987/50000 (CEF bridge/debug, irrelevant).
  The error is 100% local. Open uncertainty: whether the ai-S run already ran with
  the new server code (HTTP/1.1+`sl`, restart required) — a clean
  restart + ai-S is still outstanding.
- Restart confirmed, ai-S fails anyway. New finding: the skeleton
  contains `Inventory.ForgedItem.Item = null` (`InventoryItem` is
  abstract!), 2× ObjectId `"000…"`, 4× `""` dates. Variant
  `server/stubs/ai-C.json` (`server/make_aiC.py`) removes all three forms;
  build up additively on success, bisect the scalars on failure.
- ai-C fails too → bisect via key removal (`server/make_aiD.py`):
  `ai-D1.json` (28 keys: without all 9 object members), fallback
  `ai-D2.json` (13 scalars).
- D1 + D2 BOTH fail → even 13 scalars don't parse. New suspicion:
  response `Content-Type`: our server sent `application/json`, but the
  only JSON CT literal in the client is
  `application/json; charset=utf-8` (exactly the ASP.NET format, also in its
  requests). `logging_server.py` now sends exactly this CT (server
  restart required). Test directly with the full stub: if the CT was the cause,
  we're through in one run.
- CT theory still open. NEW LEADING THEORY (`$type`): the client parser resolves
  object deserializers via `$type`
  (`Cannot get object serializer [%s]`; no key literals in the binary!);
  the client itself sends `$type` per DTO (capture). Our responses never
  had `$type` — `{}` gets through (nothing to resolve), every non-empty
  object fails. `server/add_dollar_type.py` sets `$type` (Newtonsoft
  format, in first position) on all 521 DTO objects of the full stub
  (type names verified against dto-catalog; polymorphic `Item`s included).
  `logging_server.py` now additionally logs a response summary +
  start marker (verified) — from `requests.log.jsonl` it will be possible
  to read WHAT the client received (restart check!).
- BREAKTHROUGH: with the `$type` full stub (+ new server code) the
  account task runs through — instead of `0x80030003` now
  `Invalid AccountInformation … GetBuildInfo:279` (`m_currentAccountInfo`
  NULL). I.e. response parse OK; check storage/follow-up call. Next
  step: ignore chain (all follow-up dialogs in order) + new
  requests in the game log. The contributions of CT vs. `$type` cannot be cleanly
  separated in retrospect — both stay in (both true to the spec).
- Analysis of the storage path (static): the success handler stores via
  `AcquireResult` (`[Task+0x9B8]` → slot, move!) — slot NULL means
  TASK RESULT NULL (parse "successful", but NULL object). Hypothesis:
  a top-level `$type` confuses the lookup (silent NULL); static dispatch
  without a top-level `$type` + nested `$type` for polymorphic `.Item` could be
  the combination. Variant `ai-E.json` (`server/make_aiE.py`): full stub
  minus top-level `$type`. Three outcomes: 0x80030003 back / assert stays /
  breakthrough.
- Result ai-E: 0x80030003 back. BUT: transport fix vs. `$type` never
  cleanly decoupled (ai-E may have run with old code)! Next run:
  ai-E + VERIFIED new code (curl check `Content-Type:
  application/json; charset=utf-8`) → cleanly separates transport vs. `$type`.
- Side finding: log lines could tear with parallel requests
  (threading without a lock) → `LOG_LOCK` in `logging_server.py`.
- Ops: `server/stop.ps1` ends all three servers by command line
  (if Ctrl+C doesn't arrive in the terminal; for the port-80 process, run as
  admin if needed).

- Start `MightyQuest.exe` directly (bypassing the launcher):
  `MightyQuest.exe -server_url http://127.0.0.1:PORT -token test -email
  test@test -environmentName local -httpCompression 0`
  (omit the Steam args if needed; watch the `-compipe` child processes).
- Own logging HTTP server on PORT (dump all paths + headers + body),
  return minimal JSON responses, extend **iteratively**:
  `HealthCheck` → `ValidateToken/Login` → `GetAccountInformation` → …
- WinHTTP traffic to localhost can be intercepted without TLS; the UBX packing is
  unpacked at runtime (memory dump for strings/URLs if needed).
- Expectation: after every missing call the game shows an error with the
  service name — that is the best specification.

## D. Server rebuild (proposal: C# / ASP.NET Core or Python)

1. **Skeleton**: routing `{service}.hqs/{method}`, GET/POST dispatch,
   JSON (System.Text.Json / Newtonsoft), read headers `t/i/h/rnd/n/g`,
   error envelope, logging of all calls (crucial for iteration).
2. **Session/auth**: token table, `ValidateToken`→token,
   `[Authenticated]` equivalent, HMAC verify (Murmur3) optional.
3. **Domain stubs**: minimal sensible responses per service
   (account info with starter castle/hero from `GameplaySettings`, empty lists,
   fixed shop SKUs); attack lifecycle (`StartAttack`→`EndAttack`) with
   simple loot calculation; `SendCommands` as a no-op with echo.
4. **Persistence**: SQLite/Postgres; model from `Entities.xml` + need.
5. **Load GameplaySettings**: `Data/GameplaySettings/**/*.json` from
   `GameData.7z` as the rule base (SpecContainerIds, costs, loot tables).
6. Later: friendship/challenges/leaderboards, admin import
   (`ImportCastle`), E2E tests with a script client against all 220 endpoints.

## E. Open risks

- **Packed master**: if it contains integrity/anti-debug checks (UBX) or
  hard-coded URLs/certificates, patches or hosts/proxy tricks are needed.
  So far no indication of certificate pinning
  (plain HTTP via `-server_url` is intended).
- **CEF UI** (`mightyquest-ui.exe`): lobby/shop pages might make their own
  web calls — capture and rebuild them.
- **Chat (ejabberd/XMPP)**: `ChatSettings`/`XmppInfo` DTOs exist;
  for now deliver `ChatEnabled=false`.
- **Version**: client `0.36.1.34.0` vs. toolbox `main.348.1` — slight
  API drift possible; the dynamic capture decides.
- ROOT CAUSE FOUND (`GetBuildInfo:279`): the client expects JSON responses as
  `WrappedResponse` (`{"$type":"Util.Common.Service.WrappedResponse,
  Util.Common.Service","Result":…}`), see `30-protokoll.md`. The MQLog of the
  run with the `$type` full stub showed `Account information loaded` right
  before the assert. Schema drift client (Update3) ↔ toolbox: the client does not know,
  among others, `ShopSkus`, `Challenges`, `AttackRegions`, `Hero.Attributes`,
  `InventoryItem.Type/SellPrice`, `ItemEffect.Variation/AbsoluteValue`
  (only JSON WARNINGs, uncritical).
- Follow-on error "silent exit after 20–30 s": wrapper `$type` →
  OOB read in the client type table (procdump minidump, see `30-protokoll.md`).
  Fix: wrapper without `$type` (`{"Result":…}`), empty responses `{}`.
- Items in client form (Update3): values 1:1 from the AccountTemplates of
  the client data (ArchetypeId, PrimaryStatsModifiers, Effects{Id,Level,Type},
  PowerUp{Id,Level}); field lists from the client readers. Result: shop,
  purchase (`BuyHeroItemCommand`) and equipping (`HeroEquipmentEquipCommand`)
  work client-side; the server acknowledges `SendCommands` with `{}`, but
  doesn't store anything yet (after a restart the purchase is gone).
- Persistence: `server/account_state.py` holds the account (seed = stub,
  state `server/state/account.json`, delete = reset). Handled:
  `BuyHeroItemCommand` (SKU from `ShopSkuBaseSettings.json`),
  `HeroEquipmentEquipCommand`/`UnequipCommand`. Client HeroItemSlot
  (Update3, verified): Head1 Shoulders2 Body3 Back4 Neck5 Finger1=6
  Hands7 MainHand8 OffHand9 Costume10 Pet11. Unknown commands →
  `[state] not handled` in the server log.