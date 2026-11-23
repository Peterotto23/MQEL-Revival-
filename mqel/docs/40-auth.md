Auth & Sessions

Reconstructed from HyperQuest.GameServer.dll (Business.ConnectionManager, Util.Security.AuthenticationInterceptor, Util.Security.HmacGenerator, Util.UserStorageLoader) and HyperQuest.GameServer.Contracts (IAccountService, ISleepyGiantService).

Login flow (game client)
text
Launcher (website login) ── cookie "t" (= LoginToken), "hyperquest_launcher_session" (= SGToken)
  │  _buildUserInfoPayload() → { LoginToken, SGToken, UserEmail }
  ▼  passed as CLI args -token/-email to MightyQuest.exe
Game → POST SleepyGiantService.hqs/ValidateToken {"token": LoginToken}
     or POST SleepyGiantService.hqs/Login {profileId, accountId, ipAddress,
        languageCode, countryCode, dateOfBirth, timeZoneOffset}
  ▼
Server creates a session (Entities.Sessions.*) + ConnectionToken
  ▼
From then on the game sends the header "t: <ConnectionToken>" with every request
IAccountService.Login(email, password, ipAddress, timeZoneOffset) is the password login (launcher website, not the game client).
ConnectionTokenManager.GetOrCreateUserContext: reads t from the CallContext (copied from the request headers by UserStorageLoader), loads AuthenticatedInfoEntity (AccountId, privileges, …), caches it as UserContext. Unknown/empty → anonymous context (a/e headers) or null.
Logout() destroys the token.

Revival conclusion: We issue ConnectionTokens ourselves (e.g. after a free ValidateToken/Login: token = random string → create session). The original login against Ubiservices is dropped.

[Authenticated] (method protection)
Server-side, the AuthenticationInterceptor checks whether the target method carries [Authenticated] (AllowAnonymous, RequiredPrivileges, DisableHmac).
Without a UserContext → InvalidConnectionTokenException; anonymous despite prohibition or missing privileges → AccessDeniedException.
Mapping (56× bare + 3× with props, from HyperQuest.GameServer.dll, see analysis/authenticated-map.txt): practically all game methods (GetAccountInformation, StartAttack, EndAttack, SendCommands, Buy, …). Without the attribute (= anonymous OK): Login/ValidateToken/HealthCheck methods, KeepAlive (DisableHmac), Track/TrackBatch (AllowAnonymous, DisableHmac).
Client HMAC (anti-cheat, build flavor HW_ANTICHEAT_MASTER)

If EnableClientHmac is active and header h is present and the method is [Authenticated] without DisableHmac:

text
expected = Murmur32_seed0( UTF8(PathAndQuery) + UTF8(HttpMethod)
                           + raw request body bytes
                           + UTF8(ConnectionToken) + UTF8(RequestId decimal)
                           + UTF8("`19^sW5;*|k#1$_lKo+dR,J5)El4*l}-") )[0..4] as UInt32
HmacGenerator.GenerateHMACSignature(request, connectionToken, requestId), hash = Util.Common.Cryptography.Murmur32 = standard MurmurHash3/x86_32, seed 0 (body constants 0xCC9E2D51/0x1B873593, tail and fMix constants verified).
The client sends i = strictly increasing request ID (uint) and h = value. The server rejects on: wrong value, i older than LastRequestId-10, repetition within the last 10 HMACs (MAX_CLIENT_HMACS_IN_SESSION).
Revival: first EnableClientHmac=false (the game sends h/i anyway; they are ignored), rebuild verification later — the algorithm is available.
Further header contexts

UserStorageLoader.LoadUserInformation copies from the headers on each request: rnd (uint, & 0xAAAAAAAA → ClientHackFlags), n (int), g (string). Harmless and ignorable, but the server should tolerate unknown headers (and the rebuild should accept them for form's sake).