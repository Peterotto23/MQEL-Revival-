# Protocol: HTTP + JSON ("Argo Services")

Reconstructed from `Util.Client.dll` (URL construction, request processors),
`Util.Server.dll` (routing, dispatch, HMAC) and
`Util.Common.Service.dll` (attributes). Verified via IL disassembly
(`tools/ildump.py`).

## URL scheme

```text
{GameServerUrl}/{ServiceName}/{MethodName}[?param=urlenc(value)&…]
```

- `GameServerUrl` comes from the launcher config or `-server_url`.
- `ServiceName` = `[Service]` attribute, e.g. `AccountService.hqs`
  (**including the `.hqs` suffix**, the Argo service definition file).
- `MethodName` = interface method name, e.g. `Login`.
- Client construction (`ServiceClientFactory` → `UrlBuilder.BuildUrlFormat`):
  base = `GameServerUrl + "/" + ServiceName`, method = `"/" + Name +
  "?p0={0}&p1={1}…"` (stream parameters excluded), values via
  `String.Format` + `UrlEncode(ToString(arg))`.
- Server routing (`ServiceHttpHandler` + `ServiceRequestProcessor`):
  component `"~/" + ServiceName` (Castle Windsor), inside it a map
  `"/" + MethodName` → method processor. Unknown paths →
  `NotFoundException` (HTTP 404 as a JSON error, see below).

Example (GET):

```text
GET http://server:port/AccountService.hqs/HealthCheck
GET http://server:port/AttackSelectionService.hqs/GetAttackSelectionList?attackRegionId=3&maxSuggestionCountByLevel=5
```

## Method types (attributes in `Util.Common.Service`)

| Attribute | HTTP | Request | Response |
|---|---|---|---|
| `[JsonGet]` (54× in game contracts) | GET | Query params (`Request.Params`: QueryString+Form+Cookies+ServerVars) | `application/json` |
| `[JsonPost]` (160×) | POST | JSON body, see below | `application/json` |
| `[StreamGet]` (2×: `GetReplay`, `GetAttackAntiCheatJob`) | GET | Query params | `application/octet-stream` |
| `[StreamPost]` (1×: `StoreReplay(accountId, replayStream)`) | POST | Query + binary stream (via CallContext) | `application/json` |
| `[HtmlGet]` (1×: `LinkFacebookAccount`) | GET | — | `text/html` (not needed in the revival) |

- Wrong HTTP method → `NotFoundException`.
- `[Private]` (on type or method; among others `AdminService`,
  `SleepyGiantService` and `DashboardService` in their entirety): only via
  **private port + HMAC headers** (`content-hmac`, `Date`,
  `content-accesskeyid`, signature `GenerateHMACSignature(Url, Method,
  Secret)` in `Util.Common.Service.WebRequestHelper`). Public
  game traffic does **not** need this.
- `BodyStyle` default = **0 = "bare"**: responses are the bare result JSON
  (no wrapper). `WrappedResponse<T>` (`{"Result":…}` via the
  CallContext key `WRAPPED_RESPONSE`) is only used with an explicit
  `BodyStyle=1` — not used in the GameServer.

## POST body

`{"<paramName>": <value>, …}` — one field per method parameter
(`PostHttpBodyContractBuilder`: dynamic contract type with fields =
parameter names; deserialization via `Util.Common.Json`):

```text
POST http://server:port/AccountService.hqs/Login
Content-Type: application/json

{"email":"…","password":"…","ipAddress":"…","timeZoneOffset":60}
```

Parameter and method names: `docs/analysis/api-gameserver.txt`
(generated with `tools/apimap.py`, including parameter names from the
.NET `Param` table).

## GET parameter types

`GetHttpMethodCompiler`: `string` raw, otherwise `BuildConvertMethodCallExpression`
(`Parse`-based: numeric types, `bool`, `Enum`, `DateTime`, `ObjectId`,
`Nullable<>`; missing → `default(T)`). There are no complex DTOs via GET —
all complex calls are POST.

## Ambient headers (every request, see `40-auth.md`)

| Header | Type | Meaning |
|---|---|---|
| `t` | string | ConnectionToken (session, see Auth) |
| `i` | uint32 | Request counter (anti-replay, only with `h`) |
| `h` | uint32 | Client HMAC (Murmur3, only if the server has `EnableClientHmac`) |
| `rnd` | uint32 | Client flags (`& 0xAAAAAAAA` → `ClientHackFlags`, telemetry) |
| `n` | int | (context, check server-side usage) |
| `g` | string | (context, check server-side usage) |

The server reads them in `UserStorageLoader.LoadUserInformation` from
`Request.Headers` into the CallContext. They are optional for basic
operation (without `t` = anonymous; without `h` = no HMAC check).

## Dynamically verified (2026-10-13, client `0.36.1.34.0` against a logging server)

- The client additionally sends the header **`sl: 0`** (otherwise known only
  as a server header) as well as `Accept: */*`.
- `Content-Type` is `application/json; charset=utf-8`.
- The request counter `i` starts at **0** and rises strictly (`0, 1, …`);
  `h` is always sent along (the server ignores both for now).
- POST bodies use **Newtonsoft `$type` polymorphism**:
  `{"commands":[{"$type":"HyperQuest.GameServer.Contracts.TrackingCommand,
  HyperQuest.GameServer.Contracts","TrackingTag":{"$type":"…GameStartTracking,
  …","GameClientVersion":"280139","MachineId":"…","CreationDate":"",
  "TrackingTagId":108}}]}` — note: a default `DateTime` is serialized as
  **`""`**, `GameClientVersion` = game package ID (`version.Game.txt`).
- Stream parameters (`replayStream`) do, as expected, **not** appear in the
  JSON body (`SendCommands` sends only `{"commands":[…]}`).
- First boot flow: `POST ServerCommandService.hqs/SendCommands`
  (GameStart tracking) → `GET AccountInformationService.hqs/GetAccountInformation`.

## Versioning

- `[Service]` can carry a `ServiceVersion`; `CheckServiceVersionInterceptor`
  checks the client version (`"v"`). Details/tolerance still to be verified —
  the rebuild should accept any version.
- `Content-Length: 0` POSTs without a body are permitted (parameterless calls).

## Errors

- HTTP status = `ServiceException.HttpStatusCode`,
  `Content-Type: application/json`, body = `ServiceExceptionSerializer` JSON
  (code, message, …). The client turns this into typed exceptions
  (`InvalidConnectionTokenException` → re-login required).
- Unknown route/method → 404 JSON (`NotFoundException`).

## Timeouts / miscellaneous

- Client timeout: 1,200,000 ms; the `HttpCompression` flag (launcher config)
  suggests optional compression.
- Header `sl: <ms>` (server): random extra sleep (timing obfuscation) —
  can be ignored in the rebuild.

## Response wrapper (statically verified, 2026-10-13)

Contrary to the server default (`BodyStyle=bare`), **this client** expects
every JSON response as a `WrappedResponse`: the generic proxy handler
(`MightyQuest_release.exe` `0x5cc280`) deserializes with the fixed type
`"Util.Common.Service.WrappedResponse, Util.Common.Service"` and stores
its `Result` in the task result slot (`Task+0x9B8`). Property name
`Result` per `Util.Common.Service.dll` (`WrappedResponse`1`).

```json
{"Result":{"$type":"HyperQuest.GameServer.Contracts.AccountInformation, HyperQuest.GameServer.Contracts", …}}
```

The wrapper itself carries **no `$type`**: `WrappedResponse` is missing from
the client's type table (2293 sorted names, last one `UIContracts…`), and
the binary search (`0x1285020`) reads an entry past the end of the table
for names that sort after the last entry → access violation
(`0xE123456E`), the process ends silently (minidump confirmed). Empty
responses (void methods) stay `{}`.

A bare DTO response with `$type` → parse OK, but `Result` is NULL
(`GetBuildInfo:279`); without `$type` → `0x80030003`. `game_server.py`
wraps all JSON responses centrally (`wrap_response`).

## TimeSpan format (Update3 client)

The client's JSON reader (0x122c680) expects TimeSpans **not** as .NET `"d.hh:mm:ss"`,
but as a string of number+unit sequences, optionally prefixed with `-`: `w` (week), `d`, `h`, `m`, `s`,
in upper and lower case, result in seconds. Examples: `"7d"`, `"1h30m"`, `"0s"`.
Caution: many duration fields are not TimeSpans in the client at all, but integers (e.g. `CastleInfo.AverageDuration`,
`CastleValidationDuration`), and lists such as `CompletionReward` expect `[`, not `null`. When in doubt, check the
field reader: `tools/client_members.py` + disassembly; same reader call as a known int field = int.
With `"00:00:00"` the client reports `JSON ERROR: …: Not a time span`, aborts reading and then loads forever.

## Server notifications (state changes after actions)

The client does **not** apply the results of server-side actions (attack loot:
gold, XP, items …) on its own, and only calls `GetAccountInformation` at
startup. In the original, such changes arrived as push notifications
(~100 types, among others `WalletUpdatedNotification`, `HeroInventoryAddedNotification`,
`HeroXpChangedNotification`, `MinePillagedNotification`; base
`Notification {Index, NotificationType}`).

- `ServerNotificationController` (`0xe22710`) processes received
  notifications in order and compares `Index` with its own counter
  (`+0xcb50`) — exactly this counter goes to the server as the request header **`n`**.
  On a gap, the client calls `NotificationService.CheckNotifications`
  (void) so that the server resends what is missing.
- **The transport is the response wrapper of every HTTP response**, not XMPP: the
  client reads it with `HyperquestWrappedResponseSerializer`, fields `Result`,
  `Notifications`, `GlobalNotifications`, `AccountForValidation` (the
  toolbox `WrappedResponse`1` only knows `Result`). `ServerNotificationController`
  subscribes to the corresponding ArgoManager event (HTTP layer).
- Format: `{"Result": …, "Notifications": [{"$type": "…WalletUpdatedNotification, …",
  "Index": 1, "NotificationType": "WalletUpdated", …}]}`; `NotificationType` as
  an enum name (list in the client directly after the `…Notification` class names).
- Implementation: `server/notifications.py` (queue, `n` = acknowledgment,
  reset on `GetAccountInformation`).
- Consequence, verified 2026-10-14 with a fresh account: gold/XP/items from an
  attack only appear after a client restart (server state is correct).