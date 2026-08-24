"""
schemas.py — Pydantic request & response models
"""

import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, validator
from game_logic import DEFAULT_DISRUPTION_CONFIG, DISRUPTION_EVENT_TYPES
from models import DEFAULT_WAREHOUSE_CONFIG, DEFAULT_VEHICLE_CONFIG


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────
class WarehouseTypeConfig(BaseModel):
    purchase_cost:   float
    capacity:        int
    build_quarters:  int
    sell_back:       float
    overhead_cost:   float = 0.0

    @validator("overhead_cost")
    def _overhead_cost_non_negative(cls, v):
        # simulate_quarter subtracts this from operating_cost every quarter
        # for every active warehouse of this type — a negative value would
        # silently inflate profit with no error anywhere in the request path.
        if v < 0:
            raise ValueError("overhead_cost must be >= 0")
        return v


class VehicleTypeConfig(BaseModel):
    purchase_cost:   float
    operating_cost:  float
    capacity:        int
    sell_back:       float
    serves_urgent:   bool
    serves_nonurgent: bool


class MapSlot(BaseModel):
    id: str
    x:  float   # percentage 0–100
    y:  float   # percentage 0–100


class DisruptionEventConfig(BaseModel):
    enabled:      bool  = True
    probability:  float = 0.08   # chance this event fires in a given quarter, 0–1
    severity_min: float = 0.2
    severity_max: float = 0.5

    @validator("probability")
    def _probability_in_range(cls, v):
        if not (0 <= v <= 1):
            raise ValueError("probability must be between 0 and 1")
        return v

    @validator("severity_min")
    def _severity_min_in_range(cls, v):
        if not (0 <= v <= 1):
            raise ValueError("severity_min must be between 0 and 1")
        return v

    @validator("severity_max")
    def _severity_max_in_range_and_at_least_min(cls, v, values):
        if not (0 <= v <= 1):
            raise ValueError("severity_max must be between 0 and 1")
        lo = values.get("severity_min")
        if lo is not None and v < lo:
            raise ValueError("severity_max must be >= severity_min")
        return v


class QuarterOverride(BaseModel):
    """An instructor-pinned disaster: this exact event_key is guaranteed to
    fire on this exact quarter, at this severity — bypassing that event
    type's probability roll for that one quarter only. Other event types
    (and this same event type on non-overridden quarters) still roll
    normally per their `events` config above."""
    quarter:    int
    event_key:  str
    severity:   float = 0.3

    @validator("quarter")
    def _quarter_positive(cls, v):
        if v < 1:
            raise ValueError("quarter must be >= 1")
        return v

    @validator("event_key")
    def _event_key_known(cls, v):
        if v not in DISRUPTION_EVENT_TYPES:
            raise ValueError(f"unknown event_key: {v}")
        return v

    @validator("severity")
    def _severity_in_range(cls, v):
        if not (0 <= v <= 1):
            raise ValueError("severity must be between 0 and 1")
        return v


class DisruptionConfig(BaseModel):
    """Master switch plus per-event-type tuning. See game_logic.DISRUPTION_EVENT_TYPES
    for the fixed set of event keys this dict may contain."""
    enabled: bool = DEFAULT_DISRUPTION_CONFIG["enabled"]
    events: Dict[str, DisruptionEventConfig] = {
        key: DisruptionEventConfig(**cfg) for key, cfg in DEFAULT_DISRUPTION_CONFIG["events"].items()
    }
    quarter_overrides: List[QuarterOverride] = []

    @validator("events")
    def _backfill_missing_event_types(cls, v):
        # A caller submitting `events` with only some of the known keys (e.g.
        # the instructor dashboard always sends all three, but a direct API
        # call might not) would otherwise silently disable the omitted event
        # types going forward — roll_disruption_events() just skips whatever
        # key isn't present. Missing keys fall back to their documented
        # defaults instead of quietly dropping out of rotation.
        merged = dict(v)
        for key, cfg in DEFAULT_DISRUPTION_CONFIG["events"].items():
            if key not in merged:
                merged[key] = DisruptionEventConfig(**cfg)
        return merged


# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO schemas
# ─────────────────────────────────────────────────────────────────────────────
class ScenarioCreate(BaseModel):
    name:            str = "Untitled Scenario"
    total_quarters:  int   = 16
    starting_budget: float = 3_000_000.0

    warehouse_config: Dict[str, WarehouseTypeConfig] = {
        key: WarehouseTypeConfig(**cfg) for key, cfg in DEFAULT_WAREHOUSE_CONFIG.items()
    }

    vehicle_config: Dict[str, VehicleTypeConfig] = {
        key: VehicleTypeConfig(**cfg) for key, cfg in DEFAULT_VEHICLE_CONFIG.items()
    }

    urgent_order_revenue:    float = 60.0
    nonurgent_order_revenue: float = 27.5

    demand_min_per_zone:   int   = 133
    demand_max_per_zone:   int   = 532
    urgent_demand_ratio:   float = 0.3
    demand_reveal_start_quarter: int = 6
    warehouse_service_radius: int = 30
    allow_sell_warehouses: bool = True
    allow_sell_trucks:     bool = True
    allow_sell_drones:     bool = True
    allow_outsourcing:        bool = False
    outsource_cost_urgent:    float = 75.0
    outsource_cost_nonurgent: float = 40.0
    allow_moving_vehicles:    bool = False
    unlocked_quarter:         int = 0

    warehouse_slots:       List[MapSlot] = []
    demand_zone_positions: List[MapSlot] = []

    disruption_config: DisruptionConfig = DisruptionConfig()

    @validator("demand_max_per_zone")
    def _demand_max_at_least_min(cls, v, values):
        lo = values.get("demand_min_per_zone")
        if lo is not None and v < lo:
            raise ValueError("demand_max_per_zone must be >= demand_min_per_zone")
        return v

    @validator("unlocked_quarter")
    def _unlocked_quarter_non_negative(cls, v):
        # advance_quarter's gate check (`sc.unlocked_quarter and target_quarter >
        # sc.unlocked_quarter`) treats any negative value as truthy-and-always-
        # exceeded, permanently locking every play under the scenario with no
        # dashboard control able to set it back to a valid value.
        if v is not None and v < 0:
            raise ValueError("unlocked_quarter must be >= 0")
        return v

    @validator("disruption_config")
    def _quarter_overrides_within_total_quarters(cls, v, values):
        total = values.get("total_quarters")
        if v and total:
            for ov in v.quarter_overrides:
                if ov.quarter > total:
                    raise ValueError(
                        f"disruption_config.quarter_overrides: quarter {ov.quarter} "
                        f"exceeds total_quarters ({total})"
                    )
        return v


class ScenarioUpdate(ScenarioCreate):
    """Same fields, all optional for partial updates."""
    name:            Optional[str]   = None
    total_quarters:  Optional[int]   = None
    starting_budget: Optional[float] = None
    warehouse_config: Optional[Dict[str, WarehouseTypeConfig]] = None
    vehicle_config:   Optional[Dict[str, VehicleTypeConfig]]   = None
    urgent_order_revenue:    Optional[float] = None
    nonurgent_order_revenue: Optional[float] = None
    demand_min_per_zone:     Optional[int]   = None
    demand_max_per_zone:     Optional[int]   = None
    urgent_demand_ratio:     Optional[float] = None
    demand_reveal_start_quarter: Optional[int] = None
    warehouse_service_radius: Optional[int] = None
    allow_sell_warehouses: Optional[bool] = None
    allow_sell_trucks:     Optional[bool] = None
    allow_sell_drones:     Optional[bool] = None
    allow_outsourcing:        Optional[bool] = None
    outsource_cost_urgent:    Optional[float] = None
    outsource_cost_nonurgent: Optional[float] = None
    allow_moving_vehicles:    Optional[bool] = None
    unlocked_quarter:         Optional[int]  = None
    warehouse_slots:          Optional[List[MapSlot]] = None
    demand_zone_positions:    Optional[List[MapSlot]] = None
    disruption_config:        Optional[DisruptionConfig] = None


class ScenarioOut(BaseModel):
    id:              int
    code:            str
    name:            str
    total_quarters:  int
    starting_budget: float
    created_at:      Optional[str] = None

    warehouse_config:  Dict[str, Any]
    vehicle_config:    Dict[str, Any]

    urgent_order_revenue:    float
    nonurgent_order_revenue: float

    demand_min_per_zone:   int
    demand_max_per_zone:   int
    urgent_demand_ratio:   float
    demand_reveal_start_quarter: int
    warehouse_service_radius: int
    allow_sell_warehouses: bool
    allow_sell_trucks:     bool
    allow_sell_drones:     bool
    allow_outsourcing:        bool
    outsource_cost_urgent:    float
    outsource_cost_nonurgent: float
    allow_moving_vehicles:    bool
    unlocked_quarter:         int

    warehouse_slots:       List[Any]
    demand_zone_positions: List[Any]

    disruption_config: Dict[str, Any]

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_parse(cls, obj):
        """Parse JSON text fields from the ORM model."""
        return cls(
            id=obj.id,
            code=obj.code,
            name=obj.name,
            total_quarters=obj.total_quarters,
            starting_budget=obj.starting_budget,
            created_at=obj.created_at.isoformat() if obj.created_at else None,
            warehouse_config=json.loads(obj.warehouse_config),
            vehicle_config=json.loads(obj.vehicle_config),
            urgent_order_revenue=obj.urgent_order_revenue,
            nonurgent_order_revenue=obj.nonurgent_order_revenue,
            demand_min_per_zone=obj.demand_min_per_zone,
            demand_max_per_zone=obj.demand_max_per_zone,
            urgent_demand_ratio=obj.urgent_demand_ratio,
            demand_reveal_start_quarter=obj.demand_reveal_start_quarter,
            warehouse_service_radius=obj.warehouse_service_radius,
            allow_sell_warehouses=getattr(obj, "allow_sell_warehouses", True),
            allow_sell_trucks=getattr(obj, "allow_sell_trucks", True),
            allow_sell_drones=getattr(obj, "allow_sell_drones", True),
            allow_outsourcing=getattr(obj, "allow_outsourcing", False),
            outsource_cost_urgent=getattr(obj, "outsource_cost_urgent", 75.0),
            outsource_cost_nonurgent=getattr(obj, "outsource_cost_nonurgent", 40.0),
            allow_moving_vehicles=getattr(obj, "allow_moving_vehicles", False),
            unlocked_quarter=getattr(obj, "unlocked_quarter", 0),
            warehouse_slots=json.loads(obj.warehouse_slots),
            demand_zone_positions=json.loads(obj.demand_zone_positions),
            disruption_config=json.loads(getattr(obj, "disruption_config", None) or json.dumps(DEFAULT_DISRUPTION_CONFIG)),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  GAME SESSION schemas
# ─────────────────────────────────────────────────────────────────────────────
class PlayCreate(BaseModel):
    scenario_code: str           # student enters this
    student_name:  Optional[str] = None


class InstructorLoginRequest(BaseModel):
    username: str
    password: str


class OutsourceZoneRequest(BaseModel):
    zone_id:    str
    outsourced: bool


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH schemas
# ─────────────────────────────────────────────────────────────────────────────
class EmailCodeRequest(BaseModel):
    email: str


class EmailCodeVerify(BaseModel):
    email: str
    code:  str


# ─────────────────────────────────────────────────────────────────────────────
#  WAREHOUSE placement schemas
# ─────────────────────────────────────────────────────────────────────────────
class PlaceWarehouseRequest(BaseModel):
    slot_id:        str
    warehouse_type: str   # small / medium / large


class AddVehicleRequest(BaseModel):
    warehouse_id: int
    vehicle_type: str    # truck / drone


class SellWarehouseRequest(BaseModel):
    warehouse_id: int


class SellVehicleRequest(BaseModel):
    warehouse_id:  int
    vehicle_id:    str   # unique ID of the vehicle


# ─────────────────────────────────────────────────────────────────────────────
#  QUARTER schemas
# ─────────────────────────────────────────────────────────────────────────────
class DemandSpot(BaseModel):
    id:     str
    x:      float
    y:      float
    type:   str    # "urgent" | "nonurgent"
    orders: int


class DemandZone(BaseModel):
    zone_id:      str
    demand_spots: List[DemandSpot]
    total_urgent:    int
    total_nonurgent: int


class QuarterDemandOut(BaseModel):
    quarter:      int
    demand_zones: List[DemandZone]


class QuarterResultOut(BaseModel):
    quarter:         int
    revenue:         float
    operating_cost:  float
    profit:          float
    cash_after:      float
    orders_fulfilled: int
    orders_total:     int
    utilization_rate: float
    serving_pct:      float
    stockouts:        int
    outsource_expenses: float = 0.0
    outsource_revenue:  float = 0.0

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────────────────────────────────────
#  NORMAL MODE (no code)
# ─────────────────────────────────────────────────────────────────────────────
class NormalModeStart(BaseModel):
    student_name: Optional[str] = None
