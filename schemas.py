"""
schemas.py — Pydantic request & response models
"""

import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, validator


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────
class WarehouseTypeConfig(BaseModel):
    purchase_cost:   float
    capacity:        int
    build_quarters:  int
    sell_back:       float


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


# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO schemas
# ─────────────────────────────────────────────────────────────────────────────
class ScenarioCreate(BaseModel):
    name:            str = "Untitled Scenario"
    total_quarters:  int   = 16
    starting_budget: float = 3_000_000.0

    warehouse_config: Dict[str, WarehouseTypeConfig] = {
        "small":  WarehouseTypeConfig(purchase_cost=250_000, capacity=500,  build_quarters=1, sell_back=125_000),
        "medium": WarehouseTypeConfig(purchase_cost=500_000, capacity=1200, build_quarters=1, sell_back=250_000),
        "large":  WarehouseTypeConfig(purchase_cost=800_000, capacity=2500, build_quarters=3, sell_back=400_000),
    }

    vehicle_config: Dict[str, VehicleTypeConfig] = {
        "truck": VehicleTypeConfig(purchase_cost=20_000,  operating_cost=800, capacity=200, sell_back=12_000,  serves_urgent=False, serves_nonurgent=True),
        "drone": VehicleTypeConfig(purchase_cost=9_000,   operating_cost=800, capacity=60,  sell_back=5_400,   serves_urgent=True,  serves_nonurgent=True),
    }

    urgent_order_revenue:    float = 55.0
    nonurgent_order_revenue: float = 25.0

    demand_min_per_zone:   int   = 133
    demand_max_per_zone:   int   = 532
    urgent_demand_ratio:   float = 0.3
    demand_reveal_start_quarter: int = 6
    warehouse_service_radius: int = 30

    warehouse_slots:       List[MapSlot] = []
    demand_zone_positions: List[MapSlot] = []


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
    warehouse_slots:          Optional[List[MapSlot]] = None
    demand_zone_positions:    Optional[List[MapSlot]] = None


class ScenarioOut(BaseModel):
    id:              int
    code:            str
    name:            str
    total_quarters:  int
    starting_budget: float

    warehouse_config:  Dict[str, Any]
    vehicle_config:    Dict[str, Any]

    urgent_order_revenue:    float
    nonurgent_order_revenue: float

    demand_min_per_zone:   int
    demand_max_per_zone:   int
    urgent_demand_ratio:   float
    demand_reveal_start_quarter: int
    warehouse_service_radius: int

    warehouse_slots:       List[Any]
    demand_zone_positions: List[Any]

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
            warehouse_config=json.loads(obj.warehouse_config),
            vehicle_config=json.loads(obj.vehicle_config),
            urgent_order_revenue=obj.urgent_order_revenue,
            nonurgent_order_revenue=obj.nonurgent_order_revenue,
            demand_min_per_zone=obj.demand_min_per_zone,
            demand_max_per_zone=obj.demand_max_per_zone,
            urgent_demand_ratio=obj.urgent_demand_ratio,
            demand_reveal_start_quarter=obj.demand_reveal_start_quarter,
            warehouse_service_radius=obj.warehouse_service_radius,
            warehouse_slots=json.loads(obj.warehouse_slots),
            demand_zone_positions=json.loads(obj.demand_zone_positions),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  GAME SESSION schemas
# ─────────────────────────────────────────────────────────────────────────────
class PlayCreate(BaseModel):
    scenario_code: str           # student enters this
    student_name:  Optional[str] = None


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
    vehicle_index: int   # index in the vehicles JSON array


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

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────────────────────────────────────
#  NORMAL MODE (no code)
# ─────────────────────────────────────────────────────────────────────────────
class NormalModeStart(BaseModel):
    student_name: Optional[str] = None
