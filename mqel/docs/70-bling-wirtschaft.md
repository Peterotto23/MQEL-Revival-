# Bling Economy (implemented, switch in the web tool: Economy)

As of 2026-10-17. Goal: Blings (PremiumCash) become an **earned** currency.
Real money is out (the rights belong to Ubisoft), and free Blings (today's test shop
`server/bling_shop.py`) devalue the game. The shop and prices in the client
stay basically as they are; what changes is mainly **where** Blings come from.

## Guidelines

- Blings only for performance and progress, **not for presence**: no
  login bonuses, no streaks. Anyone who takes a break catches up with the same
  play time.
- Moderate FOMO is fine (seasonal events, league season, competitions),
  as long as the rewards are small and missing out doesn't hurt.
- Heroes stay behind Blings (long-term goal); the trial promos
  (`server/free_trials.py`, discount via level goals) stay.
- The Bling shop stays, but instead of Blings it sells the former
  real-money packs (cosmetics etc.) **for Blings**, including older ones.
- Only the server grants Blings; per-account weekly cap against
  second-account farming.

## Stocktake

### Spending (ShopSettings/ShopSkuBaseSettings.json)

5095 SKUs, of which a good 2000 have a Bling price (`CurrencyType` 1). Active SKUs:

| Type | Count | Blings (min / typical / max) | Classification |
|---|---|---|---|
| Hero | 14 | 798 / 1200 / 3012 | Long-term goal |
| CastleTheme | 18 | 99 / 850 / 1500 | Cosmetic |
| Decoration | 60 | 15 / 90 / 10000 | Cosmetic |
| HeroUpgrade | 116 | 100 / 500 / 999 | Progression |
| HeroEquipmentItem | 748 | 0 / 80 / 19998 | Progression (many also for gold) |
| Building, Creature, Trap, …Upgrade | ~640 | mostly 50–200 | Shortcut |
| Consumable | 65 | 5 / 60 / 700 | Shortcut |
| ForgeCrafted/-Reforged | 120 each | 15 / 60 / 120 | Shortcut |
| InventoryTabs, Room, ShowMoreCastle | 47 | 5–200 | Convenience |

In addition, time skips for Blings (`GeneralSettings/TimeSettings.json`).

Heroes: the shop shows all heroes for **950 Blings** (promo SKUs "PM"),
no life force (confirmed in-game). The remaining hero SKUs (1000
LifeForce, Daily Deal 798/1200, Featured 3012) are in the data but are
not offered.

### Sources in the original (without real money)

| Source | Amount | File |
|---|---|---|
| Objectives | one-time 200 + 7 × 50 | ObjectiveSettings.json |
| League season end, top places | 5–30 | SeasonalCompetitionSettings.json (`LeaderClosingPeriodRewards`) |
| Maintaining league | 1–4 per season | ditto (`LeagueMaintainReward`) |
| Competitions / challenges | 25 / 10 / 5 for places 1–3 | UbisoftCompetitionSettings.json, ChallengeSettings.json |
| Bling mine (building 16) | 1/h, storage 25, lootable | Buildings/000016 - BlingMine |
| Validation of delegated castles | 3 | AttackerRewardSettings.json |
| Achievements (36) | **no Blings** | AchievementSettings.json |

Our server already pays out all of these sources today (objectives.py,
leagues.py, competitions.py, castles.py for validation, the mine via
`HarvestMineBuildingCommand` with the original values 1/h, storage 25).
The mine's production and storage are in the building data, which the
client also displays: change via data patch (DataBin, reversible through
the patcher).

Without money, only a few hundred Blings added up in the original; a hero
(~1000) was practically unreachable. With its small storage, the Bling mine is
exactly the collect-on-a-schedule pattern we want to avoid.

### Special Packs (GeneralSettings/SpecialPackSettings.json)

75 packs: 56 "Diamond", 11 "DoubleO"/"DoubleOV2", 8 individual (among others Infinite
Pack 72, Halloween 67). Contents: equipment (60 packs), early hero access
(39), monsters (24), themes (19), hero boosts (17), inventory tabs (22),
traps, decorations, emotes, materials. **21 packs contain Blings
themselves** (e.g. "Chunk of Change" 1500), which must be removed when sold
for Blings.

## Draft (numbers for discussion)

The reference measure is **active play time**, not calendar time. Assumption for a
"normal" player: roughly 4 hours per week.

| Goal | Target time | ≈ Play hours |
|---|---|---|
| first cosmetic item (~100) | first days | 2–3 h |
| second hero (950) | 2–3 weeks | ~10 h |
| every further hero | 1–2 months | 20–30 h |
| big pack | a few weeks | 15–25 h |
| everything premium | many months | 200+ h |

This yields two income streams:

1. **One-time pot (~3000 Blings)**, distributed over achievements (~1500) and
   objectives (1550). Milestones are dropped (no client UI). Carries the start
   so that the second hero comes quickly.
2. **Ongoing ~30–40 Blings per play hour**: rare Bling loot when pillaging
   (largest share), plus league and competitions as a small bonus
   (original values roughly doubled), seasonal events with small prizes.
   Bling mine see below.

**Pack prices:** derived from the contents, not from the old euro price:
sum of the Bling prices of the individual items with a bundle discount, excluding
contained Blings; packs that mainly contain shortcuts get a surcharge or are
not offered at all. Without value (decided 2026-10-17): **early hero access**
(a relic of development time) is dropped; **emotes** stay in but don't count
toward the price (practically single-player, at most someone sees them in a
replay). That leaves nothing in 4 packs, so they are dropped: 16 "Runaway Only", 17 "Mage
Only" (hero access only), 19 "PC Gamer Bling Pack", 70 "Steam Big Bling
Pack" (Blings only).

### Bling mine

Today our server hands over the **entire fill level** when looted
(`loot.stealable_buildings`); gold/life force mines only the last hour's
production (`mine_stealable`, `MinePeriodStealableAmountInSeconds`). With a
large storage, a low-activity player would otherwise be a jackpot for attackers
and would still have to collect daily. Therefore:

- At most one hour of production is lootable (minimum
  `PremiumCashMineLossAmount` = 1), the rest stays with the owner.
- Destroying only sets the yield of this attack to zero, not the contents.
- Production 50/h (`Mine.BlingsPerHour`, range 1–1000; community poll
  2026-11-24, catching up with the boss raids), storage per the poll 400 (full
  after 8 h; `Mine.Capacity`), one hour of production (50) lootable. Earlier
  state (decided 2026-11-09): 1 bling per 4 h, storage 42.
- The period stays 1 h, the rate is carried by `ProductionValue`, so the
  client shows and the server pays the same number. States saved with
  `Mine.ProductionHours` stay readable in `History` (`balance._mine_rate`).
- Price 500 instead of 999 Blings (`Mine.Price`, data patch of SKU 15522 in
  ShopSkuBaseSettings, the server books via `balance.sku_price`): earned back after ~3–5
  weeks when collecting every 2–3 days, about 3 weeks of income for an average
  player; with 999 it was up to 24 weeks.
- Up to three Bling mines and three material mines per castle (BuildingInstanceRequirements,
  from castle heart 2), as in the original: shop SKUs 15522 (Bling mine, 500 Blings, original 999) and
  15699 (material mine, 500 Blings) with quota 3 (`special_packs.SHOP_QUOTAS`);
  pack mines (Infinite Pack, Material-O-Matic) count toward the same limit, which
  the server checks when building.

### Projection

Assumptions: loot 25 Blings per play hour; one-time pot 2500 over the
first ~60 play hours (~42/h until it is empty); league/competitions 5–60 per
week; mine 42 per week.

| Player | Play time/week | Blings/week at first | afterwards | 2nd hero (950) | further heroes |
|---|---|---|---|---|---|
| Light | 1 h | ~115 | ~72 | ~8 weeks | ~13 weeks |
| Average | 4 h | ~330 (15 weeks) | ~160 | ~3 weeks | ~6 weeks |
| Heavy | 15 h | ~1090 (4 weeks) | ~470 | < 1 week | ~2 weeks |
| Extreme | 40 h | pot empty after ~1.5 weeks | ~700 (cap) | < 1 week | ~1.5 weeks |

Everything premium (heroes, 18 themes, decorations, packs) roughly 30,000–40,000:
years for an average player, ~1.5 years for a heavy player.

### Cap

**Weekly cap instead of daily cap** (a daily cap rewards playing every day):
at most 600 Blings from loot in the last 7 days. Only affects extreme
players and farm accounts. The one-time pot and league are not capped (finite
or rare, respectively).

### Shortcuts

In play time (25 Blings/h): time skip 5 min / 1 h / 24 h = 5 / 50 / 850
Blings ≈ 12 min / 2 h / 34 h; monster or trap bought directly 50–200 ≈ 2–8 h;
material pack 50–200 ≈ 2–8 h; inventory tab 200 ≈ 8 h; delegating
validation 10 ≈ 25 min. With earned Blings, the original prices limit
themselves: **block nothing, make nothing more expensive**, no price patch
needed. Gold/life force for Blings does not exist in the data.

### Pack selection (decided 2026-10-17)

One fixed price per pack instead of the sum of the individual items; list with contents and
decision in `docs/analysis/special-packs.md`. **23 packs on offer**,
prices 300–4000 (top: 66 The Big Pack and 72 Infinite Pack at 4000 each);
Halloween (67) only during the event; 74 Beta QC Rewards as
premium 2500. Dropped: internal packs (21 All Pets, 68 Golden Pony),
duplicates and promo copies, 56 Limited Edition (equivalent value too high),
57 Sick Creatures (stay patched out). Can still be
readjusted before going live.

## Implementation

- **Tuning knobs** (`server/balance.py`, `state/<profile>/balance.json`):
  `Enabled` switch (default off) and the values, in the web tool under
  **Economy** (admin.Get/SetBalance, limits validated). So far: Bling mine.
- **Client values** (marked "client" in the web tool) arrive via the patcher in
  `settings.bin`: DataBin version `….B<CRC>`; every state stays in
  `History` so that deltas also work from older states. settings.bin is
  always in the DataBin package (original when off) so that switching off leads
  back to the original. Own LZO packer (settings_archive.lzo_compress): ~4 MB
  delta, build ~17 s on first request.
- **Bling mine:** when Enabled, castles.mine_rank delivers ProductionPeriod
  (1 h), ProductionValue (BlingsPerHour) and Capacity from balance;
  loot.stealable_buildings limits the loot to StealHours x BlingsPerHour
  (0 hours = not lootable). A newly built Bling mine starts
  empty like in the original (MineManager.AddMineStatus); harvesting reports
  EntityHarvestType PremiumCashMine (only in the final client).
- **Achievements:** amount per achievement in the web tool (default points × 2,
  together ~1500, type "UI"). Paid out once per achievement at the amount valid
  at that time, also retroactively for ones already reached, as soon as the
  Bling economy is on (`AchievementBlingsPaid` in the account, ledger
  source `achievements`). Display: UI patch 9 (ui_patches.py) puts "+N" with a
  Bling symbol under the points in the popup; the amounts live in the
  helper `achievementBlings` (hyperquest.extensions.js), so every
  change alters the GameUI version. Older GameUI states get all files
  anew (before: no patch at all anymore).
- **Objectives:** Bling amount per objective with a Bling reward (8 of them,
  type "client"): default 200 for Snottingham and the six bosses, 150 for
  Heist Part 2 (original 200 + 7 × 50 = 550, now 1550). The client displays
  them itself (ObjectiveSettings in settings.bin, repacked together with the mine);
  objectives.rewards pays the same amount. Already completed
  objectives get nothing retroactively.
- **Bling loot:** in real attacks (excluding practice, tutorial, competition),
  with `Loot.Chance` (default 40%) a random creature carries
  `Loot.Amount` Blings (default 10, up to 1000; DefenseIngredientLoot.PremiumCash).
  Where it drops is chosen by castle type: `Loot.PvP` (default on) = player
  castles incl. the bot players (CastleType User), `Loot.PvE` (default off) =
  campaign castles incl. boss dungeons, which pay via `Boss.<castle id>`
  instead (`attack.start_attack` passes `pvp=` to `loot.add_bling_drop`).
  Credited when killed (KilledCreatureIds; the client reports no separate
  ID list for Blings), shown in the report under TotalPremiumCash.
  Cap `Loot.WeeklyCap` (600) in a rolling 7-day window
  (`BlingLootLog` in the account); the remaining budget already caps the drop.
- **Boss raids (repeatable):** every won raid (CompletionType TreasureRoom) on
  a region's boss dungeon (castle ids 14-20, AttackAdvisorSettings.BossCastleId)
  pays the boss's amount from `Boss.<castle id>` (default 10, 20, ... 70, rising
  per region; server-only, no client patch), on top of the one-time quest reward
  and as often as the player clears it. Credited with the attack
  (`attack.boss_raid_blings` -> `gains["boss_bounty"]`, part of
  `TotalPremiumCash` in the report, ledger kind `boss`). Not paid for practice,
  validation, tutorial, competitions, a lost or aborted raid, or a fight the
  anticheat flagged. Optional cap `Boss.WeeklyCap` (default 0 = unlimited) in a
  rolling 7-day window (`BossBlingLog` in the account), shared by all bosses; the
  remaining budget caps the payout.
- **League and competitions:** one percentage factor each on all Bling amounts
  (default 200%, type "client"): SeasonalCompetitionSettings and
  UbisoftCompetitionSettings in settings.bin, respectively; the server calculates the same
  way (balance.scale_reward in leagues.settle and competitions.reward).
- **Bling shop:** when Enabled, `/blings` (button "Purchase blings") shows the
  packs as cards with contents and price (Economy section Shop, 0 = not on
  offer; 67 Halloween only during the event). Purchase: AccountState.buy_special_pack
  deducts and unlocks (ledger `bling-shop`); equipment,
  consumables and materials go into the inventory (without space, for free into
  the buyback) and are reported as notifications. The free
  test bookings only exist when the economy is switched off.
  Hero access: when Enabled, EarlyHeroAccess is missing from the packs, in the server
  (special_packs.pack) and in settings.bin (balance.DATA_REVISION 2), so that
  heroes come only for Blings; heroes already created stay.

## Way back

1. **Switch on the server** (like maintenance mode), off at first; off =
   today's rules.
2. **Price changes in the client** only via patcher packages (deltas against the
   Steam files); removing the package = original prices at the next start.
3. **Ledger:** every Bling credit and every pack purchase with its source, for
   evaluating the balance and for targeted corrections.
   **Implemented:** `state/<profile>/<AccountId>.blings.jsonl`, one line per
   change {Time, Delta, Balance, Source}; Source = request
   (`Service/Method (player)`), for commands `cmd:<Name>`, for the
   web tool `admin:<Method>` (AccountState._book on saving, source per
   thread in account_state.SOURCE).
4. **Backup of the save state** when switching (emergency only).

**Rule when switching back:** players keep everything they earned, including Blings.
Corrections only go forward (income, prices). Things are only taken away on
abuse (bug, exploit, second accounts), in a targeted way via the ledger. If
intervention is ever needed after all: exchange at an announced rate or
freezing, never deleting without compensation.

## Open questions

- Achievements: the client knows no reward (only title, description,
  points). Decided: hard-code it, amounts per achievement ID in the server and
  via UI patch as a line "+N Blings" in the popup
  (`template-achievement-completed-popup`).
- Bling loot: the report shows it (`TotalPremiumCash`), a visible
  drop during combat it does not; leave it that way.