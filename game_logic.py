"""
game_logic.py — Pure game logic (no DB dependencies)
"""

import json
import random
import string
from typing import List, Dict, Any

# ─────────────────────────────────────────────────────────────────────────────
#  Default map positions — single source of truth for both backend and frontend
#  Backend seeds these into the DEFAULT scenario on first run.
#  Frontend keeps a copy as fallback only (should never be needed in practice).
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_WAREHOUSE_SLOTS = [
    # Placed via farthest-point sampling within the current chicago_zones.png's
    # detected city interior, eroded inward by enough margin to clear the
    # actual rendered marker size (not just the point itself), PLUS a hard
    # 3% safety margin from the image's absolute edges. Warehouse and demand
    # points below were sampled TOGETHER in one combined pass so the two
    # sets can never collide with each other.
    # NOT portable to a different map image without re-running that fit.
    {"id":"s1", "x":58.42, "y":41.71},
    {"id":"s4", "x":51.43, "y":75.94}, {"id":"s6", "x":68.00, "y":70.00},
    {"id":"s7", "x":39.80, "y":30.39},
    {"id":"s8", "x":44.57, "y":55.48}, {"id":"s9", "x":63.50, "y":31.50}, {"id":"s10", "x":66.50, "y":62.50},
    {"id":"s11", "x":48.50, "y":25.50},
    # s12-s14: added later via the same validation approach as demand zones
    # d43-d70 above — jittered 8-14 units off an existing validated warehouse
    # slot at a random angle, rejecting any candidate landing within 6 units
    # of the already-validated points' own bounding envelope edge (32.24-
    # 77.96 x, 14.17-96.79 y) or within 4 units of any existing warehouse/
    # demand point, so each inherits its base point's confirmed-valid
    # location while staying well clear of the map boundary.
    {"id":"s12", "x":60.14, "y":55.22}, {"id":"s13", "x":39.94, "y":44.24}, {"id":"s14", "x":65.31, "y":37.14},
    # s15-s16: +30% warehouse slot density request, later trimmed ~10%
    # (dropped s17, see below) once a real image-based boundary check
    # existed to confirm the count was safe to pare back. First pass here
    # used the same jitter approach as s12-s14 but validated only against
    # the other points and a rectangular x/y envelope — insufficient, since
    # chicago_zones.png's actual city boundary is a highly irregular polygon
    # (not a rectangle), so 2 of the 3 first-pass points landed in the lake.
    # Re-validated properly: flood-filled the actual interior of the orange
    # boundary outline in chicago_zones.png into a pixel mask, eroded it
    # inward for marker-safety margin, and only accepted jittered candidates
    # whose full marker footprint falls inside that mask (in addition to
    # staying clear of every other warehouse/demand point).
    {"id":"s15", "x":59.31, "y":30.48}, {"id":"s16", "x":48.84, "y":66.39},
    # s8/s13/s14 above were nudged a few units inward from their original
    # farthest-point-sampling position — they sat right on the boundary
    # outline with no clearance at all. All three (plus s15/s16 above) were
    # re-checked against a ~1%-of-image-width buffered version of the same
    # interior mask, so every slot now keeps a small but real gap from the
    # coastline/city edge, not just technical non-overlap with it.
]

DEFAULT_DEMAND_POSITIONS = [
    {"id":"d1", "x":46.36, "y":57.13}, {"id":"d2", "x":60.41, "y":92.87}, {"id":"d3", "x":65.05, "y":58.91},
    {"id":"d4", "x":39.37, "y":14.17}, {"id":"d5", "x":58.84, "y":26.20}, {"id":"d6", "x":44.79, "y":43.49},
    {"id":"d7", "x":71.26, "y":87.17}, {"id":"d8", "x":62.13, "y":70.59}, {"id":"d9", "x":47.93, "y":22.10},
    {"id":"d10", "x":38.73, "y":65.78}, {"id":"d11", "x":50.29, "y":87.52}, {"id":"d12", "x":61.06, "y":81.73},
    {"id":"d13", "x":50.64, "y":33.69}, {"id":"d14", "x":32.24, "y":22.46}, {"id":"d15", "x":56.21, "y":52.50},
    {"id":"d16", "x":52.78, "y":65.51}, {"id":"d17", "x":69.12, "y":96.79}, {"id":"d18", "x":64.76, "y":49.91},
    {"id":"d19", "x":69.19, "y":66.31}, {"id":"d20", "x":61.70, "y":34.22}, {"id":"d21", "x":55.21, "y":18.98},
    {"id":"d22", "x":77.39, "y":82.17}, {"id":"d23", "x":40.09, "y":22.19}, {"id":"d24", "x":46.50, "y":69.88},
    {"id":"d25", "x":48.93, "y":49.91}, {"id":"d26", "x":57.70, "y":59.80}, {"id":"d27", "x":68.26, "y":80.48},
    {"id":"d28", "x":77.96, "y":89.75}, {"id":"d29", "x":43.94, "y":36.36}, {"id":"d30", "x":51.36, "y":40.91},
    {"id":"d31", "x":49.29, "y":15.24}, {"id":"d32", "x":54.35, "y":82.09}, {"id":"d33", "x":46.29, "y":28.61},
    {"id":"d34", "x":64.62, "y":87.52}, {"id":"d35", "x":58.13, "y":75.94}, {"id":"d36", "x":45.01, "y":63.46},
    {"id":"d37", "x":52.50, "y":26.92}, {"id":"d38", "x":67.26, "y":74.15}, {"id":"d39", "x":56.78, "y":87.88},
    {"id":"d40", "x":61.98, "y":64.44}, {"id":"d41", "x":54.14, "y":46.26}, {"id":"d42", "x":56.13, "y":36.27},
    # d43-d70: added when ZONE_SELECTION_RATIO moved from 1.0 to 0.6, so a
    # play's actual active-zone COUNT stays exactly where it was (60% of 70 ≈
    # the original 42) while genuinely becoming a random subset instead of
    # "the whole list every time". Not from the original image-based farthest-
    # point-sampling pass (see DEFAULT_WAREHOUSE_SLOTS above) — generated by
    # jittering a random offset (2.5-6 units, random angle) off an existing
    # validated d1-d42 point and rejecting any candidate within 3.2 units of
    # another demand zone or a warehouse slot, so every new point inherits
    # its base point's already-confirmed-valid location. Visually verified
    # against chicago_zones.png — all 28 land inside the city interior with
    # clear margin from the boundary and the lake.
    {"id":"d43", "x":58.04, "y":49.54}, {"id":"d44", "x":53.63, "y":70.52}, {"id":"d45", "x":61.53, "y":78.01},
    {"id":"d46", "x":63.17, "y":94.71}, {"id":"d47", "x":54.94, "y":32.49}, {"id":"d48", "x":42.74, "y":18.42},
    {"id":"d49", "x":64.49, "y":54.57}, {"id":"d50", "x":52.03, "y":51.59}, {"id":"d51", "x":48.87, "y":38.07},
    {"id":"d52", "x":44.71, "y":73.73}, {"id":"d53", "x":60.39, "y":38.32}, {"id":"d54", "x":55.07, "y":24.40},
    {"id":"d55", "x":52.82, "y":37.15}, {"id":"d56", "x":52.64, "y":61.13}, {"id":"d57", "x":47.65, "y":75.76},
    {"id":"d58", "x":74.80, "y":87.56}, {"id":"d59", "x":61.13, "y":87.77}, {"id":"d60", "x":65.19, "y":46.24},
    {"id":"d61", "x":65.44, "y":67.94}, {"id":"d62", "x":74.21, "y":95.88}, {"id":"d63", "x":63.62, "y":74.29},
    {"id":"d64", "x":67.39, "y":84.66}, {"id":"d65", "x":36.88, "y":20.24}, {"id":"d66", "x":49.74, "y":29.90},
    {"id":"d67", "x":51.99, "y":56.02}, {"id":"d68", "x":45.47, "y":90.69}, {"id":"d69", "x":42.80, "y":59.88},
    {"id":"d70", "x":59.46, "y":20.58},
]


# ─────────────────────────────────────────────────────────────────────────────
#  Random disruption events
#  Each event type resolves to a temporary multiplier applied only for the
#  quarter it fires — see roll_disruption_events / compute_disruption_modifiers
#  / simulate_quarter's disruption_modifiers param.
# ─────────────────────────────────────────────────────────────────────────────
DISRUPTION_EVENT_TYPES = {
    "earthquake_warehouse": {
        "kind":         "warehouse_capacity",
        "vehicle_type": None,
        "label":        "Earthquake",
        "message_template": "\U0001F30E Earthquake near slot {target_slot_id} — warehouse capacity cut {pct}% this quarter.",
    },
    "harsh_winter_drone": {
        "kind":         "vehicle_capacity",
        "vehicle_type": "drone",
        "label":        "Harsh Winter",
        "message_template": "❄️ Harsh winter — drone capacity down {pct}% this quarter.",
    },
    "fuel_hike_truck": {
        "kind":         "vehicle_cost",
        "vehicle_type": "truck",
        "label":        "Fuel Price Hike",
        "message_template": "⛽ Fuel price spike — truck operating cost up {pct}% this quarter.",
    },
}

# Off by default (fails open, same principle as REQUIRE_STUDENT_VERIFICATION) —
# an instructor must deliberately enable this in the scenario editor.
DEFAULT_DISRUPTION_CONFIG = {
    "enabled": False,
    "events": {
        "earthquake_warehouse": {"enabled": True, "probability": 0.08, "severity_min": 0.2, "severity_max": 0.5},
        "harsh_winter_drone":   {"enabled": True, "probability": 0.08, "severity_min": 0.2, "severity_max": 0.4},
        "fuel_hike_truck":      {"enabled": True, "probability": 0.08, "severity_min": 0.2, "severity_max": 0.6},
    },
}


def roll_disruption_events(
    disruption_config: Dict,
    scenario_id: int,
    quarter: int,
    warehouse_slot_ids: List[str],
) -> List[Dict]:
    """
    Deterministically roll this scenario's disruption events for `quarter`,
    seeded by (scenario_id, quarter) so every play under the same scenario
    code sees the identical roll on the identical quarter — same reasoning
    as generate_demand's per-play seed, just scoped to the scenario instead
    of the play so it's shared across every group.

    Quarter 0 is setup-only (no simulation runs), so no events ever fire
    for it. Returns [] entirely when the scenario has disruption events
    turned off (the default).

    disruption_config["quarter_overrides"], if present, lets an instructor
    pin a specific event to a specific quarter at a specific severity —
    that event fires on that quarter unconditionally, skipping its own
    probability roll for that quarter only. Every other event type (and
    the same event type on any other quarter) still rolls normally.
    """
    if not disruption_config or not disruption_config.get("enabled"):
        return []
    if quarter < 1:
        return []
    # Normalized (deduped + sorted) regardless of what shape the caller
    # passes in — the preview endpoint and the real /advance endpoint build
    # this list differently (one from a raw list comprehension, one from a
    # dict's keys), and r.choice() below consumes a different number of
    # random draws depending on list length, which would silently desync
    # the forecast a student sees from what actually fires if a scenario
    # ever ends up with a duplicate slot id (nothing currently rejects that
    # on scenario create/update).
    warehouse_slot_ids = sorted(set(warehouse_slot_ids or []))

    events_cfg = disruption_config.get("events", {})
    r = random.Random(scenario_id * 1000 + quarter)

    fired = []
    overridden_keys = set()

    # Instructor-pinned overrides for this exact quarter fire first and
    # deterministically — bypassing that event type's probability roll for
    # this quarter only. Sorted by event_key so the RNG draw sequence below
    # (r.choice for the target slot) never depends on submission order.
    quarter_overrides = disruption_config.get("quarter_overrides", []) or []
    this_quarter_overrides = sorted(
        (ov for ov in quarter_overrides if ov.get("quarter") == quarter),
        key=lambda ov: ov.get("event_key", ""),
    )
    for ov in this_quarter_overrides:
        key = ov.get("event_key")
        event_type = DISRUPTION_EVENT_TYPES.get(key)
        if not event_type or key in overridden_keys:
            continue
        overridden_keys.add(key)  # also suppresses this key's random roll below

        severity = max(0.0, min(1.0, ov.get("severity", 0.3)))
        target_slot_id = None
        if event_type["kind"] == "warehouse_capacity":
            if not warehouse_slot_ids:
                continue  # nothing to target — skip this override rather than crash
            target_slot_id = r.choice(warehouse_slot_ids)

        pct = round(severity * 100)
        message = event_type["message_template"].format(target_slot_id=target_slot_id, pct=pct)

        fired.append({
            "key":             key,
            "kind":            event_type["kind"],
            "vehicle_type":    event_type["vehicle_type"],
            "target_slot_id":  target_slot_id,
            "severity":        round(severity, 4),
            "message":         message,
        })

    # Fixed sorted-key order so the RNG draw sequence never depends on dict
    # iteration order (which Python does not guarantee is stable across
    # different in-memory dict constructions of "the same" JSON).
    for key in sorted(DISRUPTION_EVENT_TYPES.keys()):
        if key in overridden_keys:
            continue  # instructor already pinned this event type this quarter
        cfg = events_cfg.get(key)
        if not cfg or not cfg.get("enabled"):
            continue
        probability = cfg.get("probability", 0)
        if r.random() >= probability:
            continue

        severity_min = cfg.get("severity_min", 0)
        severity_max = cfg.get("severity_max", severity_min)
        severity = r.uniform(severity_min, severity_max)

        event_type = DISRUPTION_EVENT_TYPES[key]
        target_slot_id = None
        if event_type["kind"] == "warehouse_capacity":
            if not warehouse_slot_ids:
                continue  # nothing to target — skip this roll rather than crash
            target_slot_id = r.choice(warehouse_slot_ids)

        pct = round(severity * 100)
        message = event_type["message_template"].format(target_slot_id=target_slot_id, pct=pct)

        fired.append({
            "key":             key,
            "kind":            event_type["kind"],
            "vehicle_type":    event_type["vehicle_type"],
            "target_slot_id":  target_slot_id,
            "severity":        round(severity, 4),
            "message":         message,
        })

    return fired


def compute_disruption_modifiers(events: List[Dict], play_warehouses: List[Any]) -> Dict:
    """
    Turn the scenario-level fired events into THIS play's concrete
    multipliers, matching a warehouse-capacity event's target_slot_id
    against this play's own (unsold) warehouses. A play with no warehouse
    at the targeted slot is simply unaffected by that event — the event
    itself is shared, its impact is not.
    """
    warehouse_capacity_multipliers: Dict[int, float] = {}
    vehicle_capacity_multipliers = {"drone": 1.0, "truck": 1.0}
    vehicle_cost_multipliers     = {"drone": 1.0, "truck": 1.0}

    for ev in events:
        kind = ev["kind"]
        severity = ev["severity"]
        if kind == "warehouse_capacity":
            for wh in play_warehouses:
                if wh.is_sold or not wh.is_active:
                    continue
                if wh.slot_id == ev["target_slot_id"]:
                    warehouse_capacity_multipliers[wh.id] = warehouse_capacity_multipliers.get(wh.id, 1.0) * (1 - severity)
        elif kind == "vehicle_capacity":
            vtype = ev["vehicle_type"]
            if vtype in vehicle_capacity_multipliers:
                vehicle_capacity_multipliers[vtype] *= (1 - severity)
        elif kind == "vehicle_cost":
            vtype = ev["vehicle_type"]
            if vtype in vehicle_cost_multipliers:
                vehicle_cost_multipliers[vtype] *= (1 + severity)

    return {
        "warehouse_capacity_multipliers": warehouse_capacity_multipliers,
        "vehicle_capacity_multipliers":   vehicle_capacity_multipliers,
        "vehicle_cost_multipliers":       vehicle_cost_multipliers,
    }


def events_affecting_play(events: List[Dict], play_warehouses: List[Any]) -> List[Dict]:
    """
    Filter the scenario-level fired events down to just the ones that are
    actually relevant to THIS play — a warehouse_capacity event only if this
    play has an active (unsold, built) warehouse at the targeted slot; a
    vehicle_capacity/vehicle_cost event only if this play owns at least one
    unsold vehicle of the affected type (matches rules.txt's documented scope).
    """
    slot_ids = {wh.slot_id for wh in play_warehouses if not wh.is_sold and wh.is_active}
    owned_vehicle_types: set = set()
    for wh in play_warehouses:
        if wh.is_sold or not wh.is_active:
            continue
        owned_vehicle_types.update(count_vehicles(wh.vehicles).keys())

    def _relevant(ev: Dict) -> bool:
        if ev["kind"] == "warehouse_capacity":
            return ev["target_slot_id"] in slot_ids
        if ev["kind"] in ("vehicle_capacity", "vehicle_cost"):
            return ev["vehicle_type"] in owned_vehicle_types
        return True

    return [ev for ev in events if _relevant(ev)]


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────
def generate_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def generate_play_id() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


# ─────────────────────────────────────────────────────────────────────────────
#  Fixed demand zone selection & reveal scheduling
#  Called once per play, at creation time.
# ─────────────────────────────────────────────────────────────────────────────
ZONE_SELECTION_RATIO  = 0.6   # fraction of the master zone list used by a given play
INITIAL_REVEAL_RATIO  = 0.5   # fraction of the selected zones revealed at Quarter 0
                               # (the remaining 50% unlock progressively during gameplay)


def _spread_quarters(count: int, start_quarter: int, end_quarter: int) -> List[int]:
    """
    Return `count` quarter numbers spread as evenly as possible across
    [start_quarter, end_quarter] (inclusive), ascending, first at
    start_quarter and last at end_quarter.
    """
    if count <= 0:
        return []
    if count == 1 or end_quarter <= start_quarter:
        return [start_quarter] * count
    span = end_quarter - start_quarter
    return [start_quarter + round(i * span / (count - 1)) for i in range(count)]


def select_and_schedule_demand_zones(
    master_positions: List[Dict],
    total_quarters: int,
    reveal_start_quarter: int = 6,
    selection_ratio: float = ZONE_SELECTION_RATIO,
    initial_reveal_ratio: float = INITIAL_REVEAL_RATIO,
) -> List[Dict]:
    """
    Pick this play's fixed subset of demand zones and assign each a
    revealed_at_quarter. Positions are never regenerated after this —
    a zone that exists for this play always sits at the same x/y.

    - `selection_ratio` of the master list is randomly chosen for this play.
    - `initial_reveal_ratio` of that selection is revealed at Quarter 0
      (location-only, no demand yet — demand starts Quarter 1).
    - The rest reveal evenly from `reveal_start_quarter` through
      `total_quarters`, and once revealed a zone stays eligible for demand
      generation in every subsequent quarter.

    Returns: [ {id, x, y, revealed_at_quarter}, ... ]
    """
    pool = master_positions[:]
    n_select = max(1, round(len(pool) * selection_ratio))
    selected = random.sample(pool, min(n_select, len(pool)))
    random.shuffle(selected)  # randomize which ones land in the "initial" bucket

    n_initial = max(1, round(len(selected) * initial_reveal_ratio))
    n_initial = min(n_initial, len(selected))
    initial_zones    = selected[:n_initial]
    remaining_zones   = selected[n_initial:]

    reveal_quarters = _spread_quarters(len(remaining_zones), reveal_start_quarter, total_quarters)

    schedule = []
    for z in initial_zones:
        schedule.append({"id": z["id"], "x": z["x"], "y": z["y"], "revealed_at_quarter": 0})
    for z, q in zip(remaining_zones, reveal_quarters):
        schedule.append({"id": z["id"], "x": z["x"], "y": z["y"], "revealed_at_quarter": q})

    return schedule


def get_revealed_zones(demand_zone_schedule: List[Dict], current_quarter: int) -> List[Dict]:
    """All zones revealed as of current_quarter (location-only, no order counts)."""
    return [
        {"id": z["id"], "x": z["x"], "y": z["y"]}
        for z in demand_zone_schedule
        if z["revealed_at_quarter"] <= current_quarter
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Demand generation
# ─────────────────────────────────────────────────────────────────────────────
def generate_demand(
    demand_zone_positions: List[Dict],
    activated_urgent: List[Dict],
    activated_nonurgent: List[Dict],
    demand_min: int,
    demand_max: int,
    urgent_ratio: float,
    quarter: int,
    total_quarters: int = 16,
    seed: int = None,
) -> Dict:
    """
    Generate demand for the quarter, scaling the target number of active zones
    linearly from 50% of currently-revealed zones in Quarter 1 to 100% by the
    second-to-last quarter. The reveal schedule (set at play creation) controls
    how many zones are eligible each quarter; this ramp controls what fraction
    of those eligible zones actually produce demand.
    """
    r = random.Random(seed) if seed is not None else random

    pool = demand_zone_positions[:]
    by_id = {z["id"]: z for z in pool}

    # Calculate active ratio: exactly 50% in Q1 to 100% in the second-to-last quarter
    if total_quarters <= 2:
        active_ratio = 1.0
    else:
        if quarter >= total_quarters - 1:
            active_ratio = 1.0
        else:
            active_ratio = 0.5 + 0.5 * (quarter - 1) / (total_quarters - 2)

    target_active_count = max(2, int(len(pool) * active_ratio))
    target_active_count = min(target_active_count, len(pool))

    # Carry forward already-activated zones
    urgent_active    = [z for z in activated_urgent    if z["id"] in by_id]
    nonurgent_active = [z for z in activated_nonurgent if z["id"] in by_id]
    already_active_ids = {z["id"] for z in urgent_active} | {z["id"] for z in nonurgent_active}

    # Evolve the demand for already active zones
    def evolve_zone(z):
        prev_orders = z.get("orders")
        if prev_orders is None:
            prev_orders = r.randint(demand_min, demand_max)
        
        # 15% probability of a 50-80% drop (approx once in 5-8 quarters)
        if r.random() < 0.15:
            drop_pct = r.uniform(0.5, 0.8)
            new_orders = prev_orders * (1 - drop_pct)
        else:
            # 5-20% growth
            growth_pct = r.uniform(0.05, 0.20)
            new_orders = prev_orders * (1 + growth_pct)
            
        final_orders = max(10, min(int(round(new_orders)), demand_max))
        return {
            "id":     z["id"],
            "x":      z["x"],
            "y":      z["y"],
            "orders": final_orders
        }

    urgent_active = [evolve_zone(z) for z in urgent_active]
    nonurgent_active = [evolve_zone(z) for z in nonurgent_active]

    new_spots_needed = max(0, target_active_count - len(already_active_ids))

    if new_spots_needed > 0:
        candidates = [z for z in pool if z["id"] not in already_active_ids]
        new_zones = r.sample(candidates, min(new_spots_needed, len(candidates)))
        
        new_urgent_count = round(len(new_zones) * urgent_ratio)
        new_urgent_zones = new_zones[:new_urgent_count]
        new_nonurgent_zones = new_zones[new_urgent_count:]

        def init_zone(zone):
            return {
                "id":     zone["id"],
                "x":      zone["x"],
                "y":      zone["y"],
                "orders": r.randint(demand_min, demand_max)
            }

        urgent_active = urgent_active + [init_zone(z) for z in new_urgent_zones]
        nonurgent_active = nonurgent_active + [init_zone(z) for z in new_nonurgent_zones]

    def make_spot(fixed_zone, spot_type):
        return {
            "zone_id": fixed_zone["id"],
            "x":       fixed_zone["x"],
            "y":       fixed_zone["y"],
            "type":    spot_type,
            "orders":  fixed_zone["orders"],
        }

    return {
        "urgent":              [make_spot(z, "urgent")    for z in urgent_active],
        "nonurgent":           [make_spot(z, "nonurgent") for z in nonurgent_active],
        "activated_urgent":    urgent_active,
        "activated_nonurgent": nonurgent_active,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Vehicle helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_warehouse_vehicle_capacity(vehicles_json: str, vehicle_config: Dict) -> Dict:
    """
    Splits vehicle capacity into three pools:
    - dedicated_urgent:    only serves urgent (e.g. urgent-only vehicles, if any)
    - dedicated_nonurgent: only serves nonurgent (e.g. trucks)
    - flexible:            serves BOTH (e.g. drones) — a SHARED pool, not
                            double-counted toward both types. A vehicle with
                            capacity 60 that serves both can deliver at most
                            60 orders total this quarter, split however
                            fulfillment needs it — not up to 60 of each.
    """
    vehicles = json.loads(vehicles_json or "[]")
    dedicated_urgent = 0
    dedicated_nonurgent = 0
    flexible = 0
    for v in vehicles:
        if v.get("is_sold"):
            continue
        cfg = vehicle_config.get(v["type"], {})
        cap = cfg.get("capacity", 0)
        serves_urgent    = cfg.get("serves_urgent")
        serves_nonurgent = cfg.get("serves_nonurgent")
        if serves_urgent and serves_nonurgent:
            flexible += cap
        elif serves_urgent:
            dedicated_urgent += cap
        elif serves_nonurgent:
            dedicated_nonurgent += cap
    return {
        "dedicated_urgent":    dedicated_urgent,
        "dedicated_nonurgent": dedicated_nonurgent,
        "flexible":            flexible,
        "total_capacity":      dedicated_urgent + dedicated_nonurgent + flexible,
    }


def count_vehicles(vehicles_json: str) -> Dict[str, int]:
    """Return {type: count} for all active (unsold) vehicles."""
    vehicles = json.loads(vehicles_json or "[]")
    counts: Dict[str, int] = {}
    for v in vehicles:
        if not v.get("is_sold"):
            counts[v["type"]] = counts.get(v["type"], 0) + 1
    return counts


def get_assigned_vehicle_capacity(vehicles_json: str, vehicle_config: Dict) -> float:
    """
    Total orders/qtr capacity currently assigned to a warehouse, summed
    across all its unsold vehicles. Used to cap vehicle purchases at the
    warehouse's own capacity rating — you can't stock a small (500/qtr)
    warehouse with more fleet capacity than it can actually hold.
    """
    vehicles = json.loads(vehicles_json or "[]")
    total = 0.0
    for v in vehicles:
        if v.get("is_sold"):
            continue
        cfg = vehicle_config.get(v["type"], {})
        total += cfg.get("capacity", 0)
    return total


def compute_pending_vehicle_net_cost(pending_deltas: Dict, vehicle_config: Dict, allow_moving_vehicles: bool = False) -> float:
    """
    Net cash impact of this quarter's not-yet-settled vehicle buy/sell
    actions, netted PER VEHICLE TYPE (a truck sale never offsets a drone
    purchase). For each type: net = bought - sold.
      net > 0  -> charged at purchase_cost for the net increase
      net < 0  -> credited at sell_back for the net decrease
      net == 0 -> zero cost — buying and selling the same count nets to a
                  free relocation, regardless of which warehouse either
                  action happened at.
    If allow_moving_vehicles is True, netting is disabled and same-quarter buy/sells
    are not free; they face full depreciation penalty.
    Returns a single float: positive = net cost (cash goes down when
    settled), negative = net credit (cash goes up when settled).
    """
    total = 0.0
    for vtype, counts in pending_deltas.items():
        cfg = vehicle_config.get(vtype)
        if not cfg:
            continue
        if allow_moving_vehicles:
            total += counts.get("bought", 0) * cfg["purchase_cost"]
            total -= counts.get("sold", 0) * cfg["sell_back"]
        else:
            net = counts.get("bought", 0) - counts.get("sold", 0)
            if net >= 0:
                total += net * cfg["purchase_cost"]
            else:
                total += net * cfg["sell_back"]  # net is negative -> this subtracts (credit)
    return total


def compute_vehicle_capex(pending_deltas: Dict, vehicle_config: Dict, allow_moving_vehicles: bool = False) -> float:
    """
    Gross cash spent BUYING new vehicles this quarter — capital investment in
    new fleet capacity, as opposed to compute_pending_vehicle_net_cost() which
    nets buys against same-quarter sells to get the true cash impact. Never
    negative: a same-quarter buy+sell of the same type is a free relocation
    (net cost 0) when netting is on, so it contributes $0 here too, since no
    new capacity was actually added. When allow_moving_vehicles is True there
    is no netting, so every purchase counts in full even if the same type was
    also sold that quarter (each is charged/credited independently).
    """
    total = 0.0
    for vtype, counts in pending_deltas.items():
        cfg = vehicle_config.get(vtype)
        if not cfg:
            continue
        if allow_moving_vehicles:
            total += counts.get("bought", 0) * cfg["purchase_cost"]
        else:
            net = counts.get("bought", 0) - counts.get("sold", 0)
            if net > 0:
                total += net * cfg["purchase_cost"]
    return total


# ─────────────────────────────────────────────────────────────────────────────
#  Warehouse build queue advancement
#  Called at the START of each quarter before simulation runs.
# ─────────────────────────────────────────────────────────────────────────────
def advance_warehouse_builds(warehouses: List[Any], current_quarter: int, warehouse_config: Dict):
    """
    Mark warehouses as active when build time has elapsed.
    A warehouse placed at quarter B with build_quarters=N becomes active
    at the START of quarter B+N (i.e. after N full quarters have passed).
    """
    for wh in warehouses:
        if wh.is_sold or wh.is_active:
            continue
        cfg        = warehouse_config.get(wh.warehouse_type, {})
        build_time = cfg.get("build_quarters", 1)
        # active_from is the first quarter the warehouse is usable
        active_from = wh.built_at_quarter + build_time
        if current_quarter >= active_from:
            wh.is_active = True


def quarters_until_active(wh: Any, warehouse_config: Dict, current_quarter: int) -> int:
    """How many quarters until this warehouse becomes active. 0 = already active."""
    if wh.is_active or wh.is_sold:
        return 0
    cfg        = warehouse_config.get(wh.warehouse_type, {})
    build_time = cfg.get("build_quarters", 1)
    active_from = wh.built_at_quarter + build_time
    return max(0, active_from - current_quarter)


# ─────────────────────────────────────────────────────────────────────────────
#  Quarter simulation
# ─────────────────────────────────────────────────────────────────────────────
def simulate_quarter(
    demand_zones:      List[Dict],
    warehouses:        List[Any],
    warehouse_config:  Dict,
    vehicle_config:    Dict,
    urgent_revenue:    float,
    nonurgent_revenue: float,
    current_quarter:   int,
    service_radius:    float = 30.0,
    disruption_modifiers: Dict = None,
) -> Dict:
    import math

    dm = disruption_modifiers or {}
    wh_capacity_mult = dm.get("warehouse_capacity_multipliers", {})
    veh_capacity_mult = dm.get("vehicle_capacity_multipliers", {})
    veh_cost_mult = dm.get("vehicle_cost_multipliers", {})

    active_warehouses = []
    for wh in warehouses:
        if wh.is_sold or not wh.is_active:
            continue

        # Parse vehicles and their individual stats
        wh_vehicles = []
        vehicles_list = json.loads(wh.vehicles)
        for v in vehicles_list:
            if v.get("is_sold"):
                continue
            v_cfg = vehicle_config.get(v["type"], {})
            capacity_mult = veh_capacity_mult.get(v["type"], 1.0) * wh_capacity_mult.get(wh.id, 1.0)
            cost_mult = veh_cost_mult.get(v["type"], 1.0)
            wh_vehicles.append({
                "type":             v["type"],
                "capacity":         round(v_cfg.get("capacity", 0) * capacity_mult),
                "cost":             round(v_cfg.get("operating_cost", 0) * cost_mult, 2),
                "serves_urgent":    v_cfg.get("serves_urgent", False),
                "serves_nonurgent": v_cfg.get("serves_nonurgent", False),
                "used":             0
            })

        wh_cfg  = warehouse_config.get(wh.warehouse_type, {})
        wh_x = getattr(wh, "x", 0.0)
        wh_y = getattr(wh, "y", 0.0)
        active_warehouses.append({
            "id":                      wh.id,
            "slot_id":                 wh.slot_id,
            "warehouse_type":          wh.warehouse_type,
            "max_capacity":            wh_cfg.get("capacity", 0),
            "vehicles":                wh_vehicles,
            "orders_handled":          0,
            "x":                       wh_x,
            "y":                       wh_y,
        })

    total_urgent    = sum(z.get("urgent_orders", 0)    for z in demand_zones)
    total_nonurgent = sum(z.get("nonurgent_orders", 0) for z in demand_zones)
    total_demand    = total_urgent + total_nonurgent

    fulfilled_urgent    = 0
    fulfilled_nonurgent = 0

    # 1. Assign each demand zone to its nearest active warehouse (within service radius)
    wh_assigned_demand = {
        wh["id"]: {"urgent": [], "nonurgent": []}
        for wh in active_warehouses
    }

    for z in demand_zones:
        # If coordinates are missing, fall back to assigning to the first active warehouse
        if "x" not in z or "y" not in z:
            if active_warehouses:
                first_wh = active_warehouses[0]
                if z.get("urgent_orders", 0) > 0:
                    wh_assigned_demand[first_wh["id"]]["urgent"].append(z)
                if z.get("nonurgent_orders", 0) > 0:
                    wh_assigned_demand[first_wh["id"]]["nonurgent"].append(z)
            continue

        nearest_wh = None
        min_dist = float("inf")
        for wh in active_warehouses:
            dist = math.sqrt((wh["x"] - z["x"])**2 + (wh["y"] - z["y"])**2)
            if dist < min_dist:
                min_dist = dist
                nearest_wh = wh

        if nearest_wh and min_dist <= service_radius:
            if z.get("urgent_orders", 0) > 0:
                wh_assigned_demand[nearest_wh["id"]]["urgent"].append(z)
            if z.get("nonurgent_orders", 0) > 0:
                wh_assigned_demand[nearest_wh["id"]]["nonurgent"].append(z)

    # 2. Simulate fulfillment per warehouse
    for wh in active_warehouses:
        wh_demand = wh_assigned_demand[wh["id"]]
        
        # Calculate local remaining demand
        wh_remaining_urgent    = sum(z.get("urgent_orders", 0) for z in wh_demand["urgent"])
        wh_remaining_nonurgent = sum(z.get("nonurgent_orders", 0) for z in wh_demand["nonurgent"])
        
        # Phase 1: Dedicated-urgent vehicles serve local urgent first
        for v in wh["vehicles"]:
            if v["serves_urgent"] and not v["serves_nonurgent"]:
                can_u = min(v["capacity"] - v["used"], wh_remaining_urgent)
                v["used"]            += can_u
                wh_remaining_urgent  -= can_u
                wh["orders_handled"] += can_u
                fulfilled_urgent     += can_u

        # Phase 2: Dedicated-nonurgent vehicles serve local nonurgent
        for v in wh["vehicles"]:
            if not v["serves_urgent"] and v["serves_nonurgent"]:
                can_n = min(v["capacity"] - v["used"], wh_remaining_nonurgent)
                v["used"]            += can_n
                wh_remaining_nonurgent -= can_n
                wh["orders_handled"] += can_n
                fulfilled_nonurgent  += can_n

        # Phase 3: Flexible (both) vehicles serve local urgent first, then nonurgent
        for v in wh["vehicles"]:
            if v["serves_urgent"] and v["serves_nonurgent"]:
                # first urgent
                can_fu = min(v["capacity"] - v["used"], wh_remaining_urgent)
                v["used"]            += can_fu
                wh_remaining_urgent  -= can_fu
                wh["orders_handled"] += can_fu
                fulfilled_urgent     += can_fu
                
                # then nonurgent
                can_fn = min(v["capacity"] - v["used"], wh_remaining_nonurgent)
                v["used"]            += can_fn
                wh_remaining_nonurgent -= can_fn
                wh["orders_handled"] += can_fn
                fulfilled_nonurgent  += can_fn

    # Calculate overall financials and metrics
    urgent_revenue_total    = fulfilled_urgent * urgent_revenue
    nonurgent_revenue_total = fulfilled_nonurgent * nonurgent_revenue
    revenue                 = urgent_revenue_total + nonurgent_revenue_total

    operating_cost = sum(v["cost"] for wh in active_warehouses for v in wh["vehicles"])

    # Warehouse overhead: fixed cost per quarter for each active warehouse
    overhead_cost = 0.0
    for wh in warehouses:
        if wh.is_sold or not wh.is_active:
            continue
        wh_cfg = warehouse_config.get(wh.warehouse_type, {})
        overhead_cost += wh_cfg.get("overhead_cost", 0)

    operating_cost += overhead_cost

    total_fulfilled = fulfilled_urgent + fulfilled_nonurgent
    
    # Calculate vehicle capacities and usages by type
    capacity_by_type = {}
    used_by_type = {}
    for wh in active_warehouses:
        for v in wh["vehicles"]:
            vtype = v["type"]
            capacity_by_type[vtype] = capacity_by_type.get(vtype, 0) + v["capacity"]
            used_by_type[vtype]     = used_by_type.get(vtype, 0) + v["used"]

    total_vehicle_capacity = sum(capacity_by_type.values())
    utilization_rate       = (total_fulfilled / total_vehicle_capacity) if total_vehicle_capacity > 0 else 0.0
    serving_pct            = (total_fulfilled / total_demand)           if total_demand > 0           else 0.0
    profit                 = revenue - operating_cost

    drone_capacity    = capacity_by_type.get("drone", 0)
    drone_used        = used_by_type.get("drone", 0)
    drone_utilization = (drone_used / drone_capacity) if drone_capacity > 0 else 0.0

    truck_capacity    = capacity_by_type.get("truck", 0)
    truck_used        = used_by_type.get("truck", 0)
    truck_utilization = (truck_used / truck_capacity) if truck_capacity > 0 else 0.0

    urgent_stockouts    = total_urgent - fulfilled_urgent
    nonurgent_stockouts = total_nonurgent - fulfilled_nonurgent

    # Per-warehouse-type utilization: orders_handled / total physical capacity for that type
    wh_type_orders    = {}  # {type: total_orders_handled}
    wh_type_capacity  = {}  # {type: total_warehouse_physical_capacity}
    for wh in active_warehouses:
        wt = wh["warehouse_type"]
        wh_type_orders[wt]   = wh_type_orders.get(wt, 0) + wh["orders_handled"]
        wh_type_capacity[wt] = wh_type_capacity.get(wt, 0) + wh["max_capacity"]

    warehouse_type_utilization = {}
    for wt in ("small", "medium", "large"):
        cap = wh_type_capacity.get(wt, 0)
        ord_h = wh_type_orders.get(wt, 0)
        warehouse_type_utilization[wt] = round(min(ord_h / cap, 1.0), 4) if cap > 0 else None

    # Per-vehicle-type operating cost (effective cost, including any active
    # fuel-hike-style multiplier — see disruption_modifiers above)
    drone_cost = 0.0
    truck_cost = 0.0
    for wh in active_warehouses:
        for v in wh["vehicles"]:
            if v["type"] == "drone":
                drone_cost += v["cost"]
            elif v["type"] == "truck":
                truck_cost += v["cost"]

    return {
        "revenue":                    round(revenue, 2),
        "urgent_revenue":             round(urgent_revenue_total, 2),
        "nonurgent_revenue":          round(nonurgent_revenue_total, 2),
        "operating_cost":             round(operating_cost, 2),
        "overhead_cost":              round(overhead_cost, 2),
        "profit":                     round(profit, 2),
        "orders_fulfilled":           total_fulfilled,
        "orders_total":               total_demand,
        "urgent_fulfilled":           fulfilled_urgent,
        "urgent_total":               total_urgent,
        "nonurgent_fulfilled":        fulfilled_nonurgent,
        "nonurgent_total":            total_nonurgent,
        "utilization_rate":           round(min(utilization_rate, 1.0), 4),
        "fleet_capacity":             total_vehicle_capacity,
        "drone_utilization":          round(min(drone_utilization, 1.0), 4),
        "truck_utilization":          round(min(truck_utilization, 1.0), 4),
        "serving_pct":                round(min(serving_pct, 1.0), 4),
        "stockouts":                  total_demand - total_fulfilled,
        "urgent_stockouts":           urgent_stockouts,
        "nonurgent_stockouts":        nonurgent_stockouts,
        "warehouse_type_utilization": warehouse_type_utilization,
        "drone_cost":                 round(drone_cost, 2),
        "truck_cost":                 round(truck_cost, 2),
        "warehouse_breakdown": [
            {"slot_id": wh["slot_id"], "orders_handled": wh["orders_handled"]}
            for wh in active_warehouses
        ],
    }
