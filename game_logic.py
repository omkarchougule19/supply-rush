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
    {"id":"s1", "x":58.42, "y":41.71}, {"id":"s2", "x":79.17, "y":96.97}, {"id":"s3", "x":23.68, "y":15.69},
    {"id":"s4", "x":51.43, "y":75.94}, {"id":"s5", "x":54.85, "y":11.05}, {"id":"s6", "x":73.54, "y":75.22},
    {"id":"s7", "x":39.80, "y":30.39},
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
]


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
ZONE_SELECTION_RATIO  = 1.0   # fraction of the master zone list used by a given play
INITIAL_REVEAL_RATIO  = 0.7   # fraction of the selected zones revealed at Quarter 0


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
    linearly from 50% in Quarter 1 to 100% in the final quarter (eliminating
    all golden circles by game completion).
    """
    if seed is not None:
        random.seed(seed)

    pool = demand_zone_positions[:]
    by_id = {z["id"]: z for z in pool}

    # Calculate active ratio: starts at 50% in Q1 and increases linearly to 100% by 75% progress (Q12)
    total_q = max(1, total_quarters)
    current_q = max(1, min(quarter, total_q))
    progress = current_q / total_q
    
    if progress >= 0.75:
        active_ratio = 1.0
    else:
        active_ratio = 0.5 + 0.5 * (progress / 0.75)

    target_active_count = max(2, int(len(pool) * active_ratio))
    target_active_count = min(target_active_count, len(pool))

    # Carry forward already-activated zones
    urgent_active    = [z for z in activated_urgent    if z["id"] in by_id]
    nonurgent_active = [z for z in activated_nonurgent if z["id"] in by_id]
    already_active_ids = {z["id"] for z in urgent_active} | {z["id"] for z in nonurgent_active}

    new_spots_needed = max(0, target_active_count - len(already_active_ids))

    if new_spots_needed > 0:
        candidates = [z for z in pool if z["id"] not in already_active_ids]
        new_zones = random.sample(candidates, min(new_spots_needed, len(candidates)))
        
        new_urgent_count = round(len(new_zones) * urgent_ratio)
        new_urgent_zones = new_zones[:new_urgent_count]
        new_nonurgent_zones = new_zones[new_urgent_count:]

        def fix_position(zone):
            return {
                "id": zone["id"],
                "x":  zone["x"],
                "y":  zone["y"],
            }

        urgent_active = urgent_active + [fix_position(z) for z in new_urgent_zones]
        nonurgent_active = nonurgent_active + [fix_position(z) for z in new_nonurgent_zones]

    def make_spot(fixed_zone, spot_type):
        return {
            "zone_id": fixed_zone["id"],
            "x":       fixed_zone["x"],
            "y":       fixed_zone["y"],
            "type":    spot_type,
            "orders":  random.randint(demand_min, demand_max),
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
    vehicles = json.loads(vehicles_json)
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


def get_vehicle_operating_cost(vehicles_json: str, vehicle_config: Dict) -> float:
    vehicles = json.loads(vehicles_json)
    total = 0.0
    for v in vehicles:
        if v.get("is_sold"):
            continue
        cfg = vehicle_config.get(v["type"], {})
        total += cfg.get("operating_cost", 0)
    return total


def count_vehicles(vehicles_json: str) -> Dict[str, int]:
    """Return {type: count} for all active (unsold) vehicles."""
    vehicles = json.loads(vehicles_json)
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
    vehicles = json.loads(vehicles_json)
    total = 0.0
    for v in vehicles:
        if v.get("is_sold"):
            continue
        cfg = vehicle_config.get(v["type"], {})
        total += cfg.get("capacity", 0)
    return total


def compute_pending_vehicle_net_cost(pending_deltas: Dict, vehicle_config: Dict) -> float:
    """
    Net cash impact of this quarter's not-yet-settled vehicle buy/sell
    actions, netted PER VEHICLE TYPE (a truck sale never offsets a drone
    purchase). For each type: net = bought - sold.
      net > 0  -> charged at purchase_cost for the net increase
      net < 0  -> credited at sell_back for the net decrease
      net == 0 -> zero cost — buying and selling the same count nets to a
                  free relocation, regardless of which warehouse either
                  action happened at.
    Returns a single float: positive = net cost (cash goes down when
    settled), negative = net credit (cash goes up when settled).
    """
    total = 0.0
    for vtype, counts in pending_deltas.items():
        cfg = vehicle_config.get(vtype)
        if not cfg:
            continue
        net = counts.get("bought", 0) - counts.get("sold", 0)
        if net >= 0:
            total += net * cfg["purchase_cost"]
        else:
            total += net * cfg["sell_back"]  # net is negative -> this subtracts (credit)
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
) -> Dict:
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
            wh_vehicles.append({
                "type":             v["type"],
                "capacity":         v_cfg.get("capacity", 0),
                "serves_urgent":    v_cfg.get("serves_urgent", False),
                "serves_nonurgent": v_cfg.get("serves_nonurgent", False),
                "used":             0
            })

        wh_cfg  = warehouse_config.get(wh.warehouse_type, {})
        active_warehouses.append({
            "id":                      wh.id,
            "slot_id":                 wh.slot_id,
            "warehouse_type":          wh.warehouse_type,
            "max_capacity":            wh_cfg.get("capacity", 0),
            "vehicles":                wh_vehicles,
            "orders_handled":          0,
        })

    total_urgent    = sum(z["urgent_orders"]    for z in demand_zones)
    total_nonurgent = sum(z["nonurgent_orders"] for z in demand_zones)
    total_demand    = total_urgent + total_nonurgent

    fulfilled_urgent    = 0
    fulfilled_nonurgent = 0
    remaining_urgent    = total_urgent
    remaining_nonurgent = total_nonurgent

    # Phase 1: Dedicated-urgent vehicles serve urgent first
    for wh in active_warehouses:
        for v in wh["vehicles"]:
            if v["serves_urgent"] and not v["serves_nonurgent"]:
                can_u = min(v["capacity"] - v["used"], remaining_urgent)
                v["used"]            += can_u
                remaining_urgent     -= can_u
                wh["orders_handled"] += can_u
                fulfilled_urgent     += can_u

    # Phase 2: Dedicated-nonurgent vehicles serve nonurgent
    for wh in active_warehouses:
        for v in wh["vehicles"]:
            if not v["serves_urgent"] and v["serves_nonurgent"]:
                can_n = min(v["capacity"] - v["used"], remaining_nonurgent)
                v["used"]            += can_n
                remaining_nonurgent  -= can_n
                wh["orders_handled"] += can_n
                fulfilled_nonurgent  += can_n

    # Phase 3: Flexible (both) vehicles serve urgent first, then nonurgent
    for wh in active_warehouses:
        for v in wh["vehicles"]:
            if v["serves_urgent"] and v["serves_nonurgent"]:
                # first urgent
                can_fu = min(v["capacity"] - v["used"], remaining_urgent)
                v["used"]            += can_fu
                remaining_urgent     -= can_fu
                wh["orders_handled"] += can_fu
                fulfilled_urgent     += can_fu
                
                # then nonurgent
                can_fn = min(v["capacity"] - v["used"], remaining_nonurgent)
                v["used"]            += can_fn
                remaining_nonurgent  -= can_fn
                wh["orders_handled"] += can_fn
                fulfilled_nonurgent  += can_fn

    # Calculate overall financials and metrics
    urgent_revenue_total    = fulfilled_urgent * urgent_revenue
    nonurgent_revenue_total = fulfilled_nonurgent * nonurgent_revenue
    revenue                 = urgent_revenue_total + nonurgent_revenue_total

    operating_cost = sum(
        get_vehicle_operating_cost(wh.vehicles, vehicle_config)
        for wh in warehouses
        if not wh.is_sold and wh.is_active
    )
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

    # Per-warehouse-type utilization: orders_handled / total vehicle capacity for that type
    wh_type_orders    = {}  # {type: total_orders_handled}
    wh_type_capacity  = {}  # {type: total_vehicle_capacity}
    for wh in active_warehouses:
        wt = wh["warehouse_type"]
        wh_cap = sum(v["capacity"] for v in wh["vehicles"])
        wh_type_orders[wt]   = wh_type_orders.get(wt, 0) + wh["orders_handled"]
        wh_type_capacity[wt] = wh_type_capacity.get(wt, 0) + wh_cap

    warehouse_type_utilization = {}
    for wt in ("small", "medium", "large"):
        cap = wh_type_capacity.get(wt, 0)
        ord_h = wh_type_orders.get(wt, 0)
        warehouse_type_utilization[wt] = round(min(ord_h / cap, 1.0), 4) if cap > 0 else None

    # Per-vehicle-type operating cost
    drone_cost = 0.0
    truck_cost = 0.0
    for wh in warehouses:
        if wh.is_sold or not wh.is_active:
            continue
        for v in json.loads(wh.vehicles):
            if v.get("is_sold"):
                continue
            v_cfg = vehicle_config.get(v["type"], {})
            op = v_cfg.get("operating_cost", 0)
            if v["type"] == "drone":
                drone_cost += op
            elif v["type"] == "truck":
                truck_cost += op

    return {
        "revenue":                    round(revenue, 2),
        "urgent_revenue":             round(urgent_revenue_total, 2),
        "nonurgent_revenue":          round(nonurgent_revenue_total, 2),
        "operating_cost":             round(operating_cost, 2),
        "profit":                     round(profit, 2),
        "orders_fulfilled":           total_fulfilled,
        "orders_total":               total_demand,
        "urgent_fulfilled":           fulfilled_urgent,
        "urgent_total":               total_urgent,
        "nonurgent_fulfilled":        fulfilled_nonurgent,
        "nonurgent_total":            total_nonurgent,
        "utilization_rate":           round(min(utilization_rate, 1.0), 4),
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
