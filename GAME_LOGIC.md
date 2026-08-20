# Supply Rush — How The Game Actually Works

This is a plain-English walkthrough of the game's real behavior, written by reading the backend and frontend code directly (not just the design spec in [rules.txt](rules.txt)) — cross-checked against it, and against `rules.txt` Section 9's dev-log history for *why* things are the way they are. Where the two disagreed, the code was treated as ground truth and `rules.txt`/comments were corrected to match (see the "Cleanup: Full-Codebase Logic Audit" entry at the end of `rules.txt`'s dev log for the full list).

For the numeric spec tables (costs, capacities, formulas), `rules.txt` remains the reference. This document explains the *behavior* those numbers produce.

---

## 1. Two ways to play

**Normal Mode** — click "Play Now", no code needed. You get a solo play against a fixed built-in scenario (called `DEFAULT` internally). Every time someone starts a normal-mode play, the backend double-checks that scenario's warehouse/vehicle config and map positions still match a hardcoded target and silently repairs them if not — so Normal Mode can never accidentally drift from its intended balance, even across database migrations.

**Class Code Mode** — an instructor creates a scenario in the dashboard, gets a 6-character code, shares it with students. A student enters the code (optionally with a Group Name) to join.

Both modes are backed by the exact same simulation — Normal Mode is just a fixed, always-available scenario.

## 2. Groups, not individuals

If an instructor has turned on student email verification (`REQUIRE_STUDENT_VERIFICATION`, off by default), every student verifies a school email with a one-time 6-digit code before they can play. There are never passwords — just a signed cookie that remembers you're verified for 7 days.

Whether or not verification is on, a play under a class code can be **shared by a group**: everyone who enters the same scenario code *and the same Group Name* (case-insensitive — "TeamA" and "teama" are the same group) lands in the same shared game. There's no invite link and nothing stored in the browser — re-entering the same two strings is also how you reconnect after closing the tab. Once verification is on, each verified student who joins becomes a tracked member, capped at a configurable group size (default 60); anyone acting on a play must be a member of it.

When verification is off, Group Name joining still works (a genuinely useful bonus for classes that don't need identity verification) — it's only the "must be a verified member" enforcement that switches on with the flag.

## 3. The quarter loop

A play always starts in **Quarter 0 — Setup**. No demand exists yet; you can only place warehouses and stock them with vehicles, spending against your starting budget. Clicking "Start Game" does two things: every warehouse you placed gets credited with exactly one quarter of build progress (so a 1-quarter-build small warehouse is already finished the instant you land in Quarter 1), and you move to Quarter 1. There's no results screen for this transition — nothing was actually simulated.

From Quarter 1 onward, every "Run Quarter" click:
1. Settles whatever vehicle purchases/sells you queued up during planning (see §5).
2. Generates this quarter's demand (see §4) — unless an instructor has capped which quarter you're allowed to run (§8), in which case the button is simply disabled with no error.
3. Rolls any random disruption events for this quarter (see §7).
4. Runs the actual fulfillment simulation (see §6) and books revenue, cost, and profit.
5. Advances you to the next quarter, or ends the game if that was the last one.

The game ends after the scenario's configured total quarter count (16 by default). A completed play rejects every further mutating action (buying/selling warehouses or vehicles, running another quarter) with an error — you can still view it, just not change it.

## 4. Demand: where orders come from

Each scenario has a master list of 70 possible demand-zone locations on the map. When a play is created, it randomly selects its own 60% subset of these (42 zones, by design — see "What was checked and fixed" below) and assigns each a "reveal quarter": half are visible from the start (Quarter 0, as dashed outline markers for planning only — they produce no orders yet), and the other half unlock gradually starting at a configurable quarter (Quarter 6 by default) through the end of the game. This zone map, once generated, never changes for that play — a zone's position is fixed forever, only *whether it's currently revealed* changes. Two different plays get two different random 42-zone subsets of the same 70-zone master list.

Each quarter, the game decides how many of the currently-revealed zones actually produce demand this quarter — starting at 50% in Quarter 1 and ramping linearly to 100% by the second-to-last quarter, so the map fills in gradually rather than hitting you with full complexity immediately. A zone that's ever been "activated" (produced demand once) stays active for the rest of the game; it can still shrink or grow, but it doesn't disappear.

An active zone's order count evolves quarter to quarter: 85% chance it grows 5–20% from last quarter, 15% chance it drops 50–80% (representing a sudden demand collapse), clamped between a floor of 10 and the scenario's configured ceiling. A newly-activated zone starts at a random value in the scenario's configured min/max range, and is randomly assigned "urgent" or "non-urgent" in roughly the scenario's configured ratio.

This whole process is deterministic given the same inputs — it's seeded by a number derived from the play's own database ID and the quarter number, so previewing "what will next quarter look like" (which the game does before you commit to running it) always exactly matches what actually happens when you click Run Quarter. Because the seed is per-*play*, two different groups under the same scenario code get completely independent demand patterns — only members of the *same* group (same shared play) see identical numbers.

## 5. Warehouses and vehicles

Three warehouse types (small/medium/large) differ in purchase cost, throughput capacity, build time, a fixed overhead cost charged every quarter it's active (regardless of order volume — this is what discourages over-building), and sell-back value. A warehouse placed in quarter B becomes usable at the start of quarter B + its build time; while under construction it costs nothing beyond the upfront purchase and earns nothing.

Two vehicle types (truck/drone) get stationed at a warehouse and are what actually deliver orders. A warehouse can only hold as much *total vehicle capacity* as its own capacity rating — you can't stock a 500-capacity small warehouse with more than 500 capacity worth of trucks and drones combined. Trucks only serve non-urgent orders; drones serve both, but their capacity is a single shared pool (a drone rated for 60 orders/quarter can deliver 60 total, split however fulfillment needs it — not 60 of each).

Vehicle purchases and sales during a quarter are *pending* — the cash isn't actually charged or credited until you click Run Quarter. At that moment, each vehicle type's buys and sells that quarter are netted against each other: buying and selling the same number of, say, trucks in one quarter is a free relocation (net cost $0), because nothing new was actually added. If an instructor turns on "Allow moving trucks & drones," this netting is disabled — every purchase costs full price and every sale only returns the sell-back value, with no same-quarter discount, imposing a real depreciation penalty on relocating fleet mid-quarter.

Selling a warehouse auto-sells every vehicle still parked there — refunding sell-back value for vehicles bought in a prior quarter, or simply canceling the pending purchase (no cost incurred at all) for one bought this same quarter.

## 6. Running the simulation

When a quarter actually runs, every currently-active (built, not sold) warehouse is matched to nearby demand: each demand zone is assigned to whichever active warehouse is geographically nearest, but only if that warehouse is within the scenario's configured service radius — a zone too far from every warehouse simply goes unserved (a stockout).

Within each warehouse, fulfillment happens in three passes: first, vehicles that *only* serve urgent orders handle the local urgent backlog; then vehicles that *only* serve non-urgent orders handle the local non-urgent backlog; finally, flexible vehicles (drones) mop up whatever's left of both, urgent first. Anything that can't be covered by available vehicle capacity is a stockout for that zone this quarter.

Revenue is a fixed price-per-order-delivered (urgent orders pay more than non-urgent). Operating cost is the sum of every active vehicle's per-quarter operating cost, plus every active warehouse's fixed overhead, plus any outsourcing expense (§7 below). Profit is revenue minus operating cost, and gets added straight to your cash balance — there's no separate "loan" or debt mechanism, so cash can go negative if you overspend against fulfillment. The game doesn't stop you from going negative, but the UI locks the Run Quarter button and shows a "Resolve Debt" warning until you sell something to get back above zero.

## 7. Third-party outsourcing and disruption events

**Outsourcing (3PL):** if the scenario allows it, you can toggle individual demand zones to be handled by a third party instead of your own fleet. Outsourced zones are always 100% fulfilled instantly at a configured cost-per-order, completely bypassing warehouse/vehicle capacity — useful for covering gaps your own network can't reach, at a price. A zone stays outsourced across quarters until you toggle it back off.

**Random disruption events** (new, off by default per scenario): an instructor can enable up to three kinds of quarter-scoped shocks — an earthquake that cuts a *specific warehouse map location's* effective capacity, a harsh winter that cuts every drone's capacity fleet-wide, and a fuel price hike that raises every truck's operating cost fleet-wide. Each is independently rolled every quarter with a configurable probability and severity range. Critically, the roll is shared by scenario and quarter, not by individual play — every group under one class code faces the *identical* event on the *identical* quarter (same slot, same severity), so no group gets an easier or harder deal by chance. Whether that event actually *hurts* a given group still depends on their own choices: the earthquake only matters to a group that happens to have a warehouse at the targeted map slot. This is surfaced as a forecast on the same screen where you already see next quarter's demand, before you commit to running it — a genuine "prepare or don't" decision, not a surprise buried in the results, and a toast notification also pops up for a few seconds the moment that quarter's screen loads so no group can miss it.

Beyond the random rolls, an instructor can also **pin a specific disaster to a specific quarter** (`quarter_overrides` in the scenario's disruption config) — e.g. guarantee the earthquake hits exactly Quarter 5 at 40% severity, independent of its configured probability. A pinned event always fires on its assigned quarter, skipping that event type's own random roll for that quarter only; every other event type (and that same event type on non-pinned quarters) keeps rolling normally. This is how an instructor scripts a specific case-study beat into the run instead of leaving it to chance.

## 8. Instructor tools

The dashboard (HTTP Basic Auth protected, independent of student verification) lets an instructor: create/edit/delete scenarios with full control over every number above; view every play across every scenario with search and filters; and pace the class with **quarter gating** — a scenario-wide dial (`unlocked_quarter`) that, once turned on, prevents any group from running a quarter beyond whatever the instructor has released, keeping the whole class in lockstep. Students see a simply-disabled button with a lock icon, not an error; the backend also independently rejects any attempt to run past the gate as a safety net against a stale open tab.

## 9. When the backend is unreachable

If the very first request to start a play fails outright (backend down), the student game falls back to a **fully client-side offline demo** — its own copy of the simulation logic runs in the browser with no persistence, clearly a "not the real thing" fallback (a warning banner shows). This offline mode does not know about random disruption events, quarter gating, or the more precise per-vehicle-timing nuances of selling a warehouse (it refunds every vehicle's full sell-back value regardless of when it was bought, rather than distinguishing a same-quarter cancel from a real refund) — small, deliberate simplifications in an already-approximate fallback path.

---

## What was checked and fixed in this pass

Reading through every backend module (`auth.py`, `database.py`, `models.py`, `schemas.py`, `game_logic.py`, `routes.py`, `main.py`) and both frontend files (`game.html`, `instructor_dashboard.html`) end to end surfaced seven real inconsistencies between what the code *should* do (per its own comments, or per the equivalent logic elsewhere in the same codebase) and what it actually did. All seven are fixed and verified — see the "Cleanup: Full-Codebase Logic Audit" entry in `rules.txt`'s development log for the full technical detail on each:

1. Warehouse purchases could under-count pending vehicle costs under the "Allow moving trucks & drones" setting, permitting a purchase that shouldn't have been affordable.
2. The offline demo fallback's vehicle prices were stale — up to 10x the real game's numbers.
3. The offline demo fallback's map data was empty, making that path unplayable if it were ever truly needed.
4. A dead, redundant conditional in the database connection setup.
5. Instructor play search couldn't find plays by their group name.
6. The play-detail modal showed "Anonymous" for group plays instead of the group name and roster.
7. A stale code comment describing zone-selection percentages that no longer matched reality — and, on follow-up, an actual design gap it had been masking: `ZONE_SELECTION_RATIO` was `1.0`, meaning every play used the *entire* master zone list rather than a genuine random subset. Per your decision, the master list was expanded from 42 to 70 zones (28 new points added, visually verified against the map image) and the ratio set to `0.6`, so a play still sees exactly 42 zones (unchanged, as you wanted) but now as a real random draw from a larger pool.

None of these change how a normally-functioning game (backend reachable, default settings) behaves for a student today — they were either dormant fallback-path bugs, an edge-case affordability check, documentation/UX gaps, or (for #7) a deliberate, confirmed design change.

## Open questions

- **Offline demo mode's warehouse-sale simplification** (full sell-back refund regardless of purchase timing) is a known, small divergence from the real backend behavior. Left as-is since it only affects the already-approximate offline fallback — but flagging in case you'd rather it match exactly.
- **The `DEFAULT` (Normal Mode) scenario force-repairs itself** to hardcoded values every time someone starts a normal-mode play, if its config has drifted at all. This is existing, pre-session behavior (not something I changed) — it protects Normal Mode from ever drifting, but also means it can never be customized independently without changing the hardcoded target in `routes.py` itself. Flagging only so it's a known, deliberate constraint rather than a surprise if anyone ever wants a *tunable* Normal Mode.
