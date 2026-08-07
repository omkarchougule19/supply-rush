"""
models.py — All database tables for Supply Rush
"""

import json
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO  (created by instructor, identified by a short code)
# ─────────────────────────────────────────────────────────────────────────────
class Scenario(Base):
    __tablename__ = "scenarios"

    id              = Column(Integer, primary_key=True, index=True)
    code            = Column(String(8), unique=True, index=True, nullable=False)
    name            = Column(String(120), nullable=False, default="Untitled Scenario")
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # ── Game structure ────────────────────────────────────────────────────────
    total_quarters  = Column(Integer, default=16)
    starting_budget = Column(Float,   default=3_000_000.0)

    # ── Warehouse costs & specs ───────────────────────────────────────────────
    warehouse_config = Column(Text, default=json.dumps({
        "small":  {"purchase_cost": 250_000, "capacity": 500,  "build_quarters": 1, "sell_back": 125_000},
        "medium": {"purchase_cost": 500_000, "capacity": 1200, "build_quarters": 2, "sell_back": 250_000},
        "large":  {"purchase_cost": 800_000, "capacity": 2500, "build_quarters": 4, "sell_back": 400_000},
    }))

    # ── Vehicle costs & specs ─────────────────────────────────────────────────
    vehicle_config = Column(Text, default=json.dumps({
        "truck": {"purchase_cost": 20_000,  "operating_cost": 800, "capacity": 200, "sell_back": 12_000,  "serves_urgent": False, "serves_nonurgent": True},
        "drone": {"purchase_cost": 9_000,   "operating_cost": 800, "capacity": 60,  "sell_back": 5_400,   "serves_urgent": True,  "serves_nonurgent": True},
    }))

    # ── Order pricing ─────────────────────────────────────────────────────────
    urgent_order_revenue    = Column(Float, default=55.0)
    nonurgent_order_revenue = Column(Float, default=25.0)

    # ── Demand parameters ─────────────────────────────────────────────────────
    demand_min_per_zone  = Column(Integer, default=133)
    demand_max_per_zone  = Column(Integer, default=532)
    urgent_demand_ratio  = Column(Float,   default=0.3)

    # Fixed demand zone reveal system:
    # 60% of demand_zone_positions is randomly selected per play; 70% of that
    # selection is revealed at Quarter 0, the rest reveal evenly starting at
    # this configurable quarter through the end of the game.
    demand_reveal_start_quarter = Column(Integer, default=6)

    # Max distance (in the same 0-100 x/y coordinate space as all map
    # positions) between a demand zone and a warehouse for that warehouse to
    # serve it — controls route lines, delivery animation, and the
    # capacity/demand label. Instructor-configured only; not shown/adjustable
    # in the student game.
    warehouse_service_radius = Column(Integer, default=30)

    # ── Map layout ────────────────────────────────────────────────────────────
    warehouse_slots       = Column(Text, default=json.dumps([]))  # [{id, x, y}, ...]
    demand_zone_positions = Column(Text, default=json.dumps([]))  # [{id, x, y}, ...]

    # ── Relationships ─────────────────────────────────────────────────────────
    plays = relationship("Play", back_populates="scenario")


# ─────────────────────────────────────────────────────────────────────────────
#  PLAY  (one full play-through by a student)
#
#  Replaces the old GameSession model. Key design decisions:
#  - play_id: short 8-char unique ID shown to users (e.g. "A3F9C2D1")
#  - scenario_code: stored directly — null for normal mode, code for instructor mode
#  - quarterly_results: JSON array bundling all quarter snapshots for easy
#    frontend delivery; QuarterResult table is the source of truth but this
#    stays in sync for fast reads (e.g. instructor plays view)
# ─────────────────────────────────────────────────────────────────────────────
class Play(Base):
    __tablename__ = "plays"

    id              = Column(Integer, primary_key=True, index=True)
    play_id         = Column(String(8),  unique=True, index=True, nullable=False)  # short human-readable ID
    scenario_code   = Column(String(8),  nullable=True, index=True)                # null = normal mode
    scenario_id     = Column(Integer, ForeignKey("scenarios.id"), nullable=True)   # null = normal mode
    student_name    = Column(String(120), nullable=True)
    started_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    current_quarter = Column(Integer, default=0)   # 0 = setup phase, no demand/simulation; 1..total_quarters = play
    cash            = Column(Float, nullable=False)
    completed       = Column(Boolean, default=False)

    # This play's fixed subset of demand zones (60% of the scenario's master
    # demand_zone_positions, randomly chosen once at play creation) plus each
    # zone's reveal quarter. Positions never move/get replaced once chosen —
    # only which zones are "revealed" (eligible for demand) changes over time.
    # Format: [ {id, x, y, revealed_at_quarter}, ... ]
    demand_zone_schedule = Column(Text, default=json.dumps([]))

    # Zones that have ever produced demand — once a zone is activated it
    # keeps producing demand every quarter for the rest of the game (never
    # goes quiet again). New zones can still be added on top over time.
    # Format: {"urgent": [zone_id, ...], "nonurgent": [zone_id, ...]}
    activated_demand_zones = Column(Text, default=json.dumps({"urgent": [], "nonurgent": []}))

    # Vehicle buy/sell actions taken THIS quarter, before "Run Quarter" is
    # pressed. Cash for these is NOT charged/credited immediately — only the
    # NET (bought - sold) per vehicle type is settled when the quarter is
    # actually run, so relocating a vehicle between warehouses within the
    # same planning window costs nothing. Resets to empty every time a
    # quarter is run — no netting carries across quarter boundaries.
    # Format: {"truck": {"bought": N, "sold": M}, "drone": {...}}
    pending_vehicle_deltas = Column(Text, default=json.dumps({}))

    # Bundled quarterly results for fast reads — kept in sync with QuarterResult table
    # Format: [ { quarter, revenue, operating_cost, profit, cash_after,
    #             orders_fulfilled, orders_total, utilization_rate, serving_pct, stockouts }, ... ]
    quarterly_results = Column(Text, default=json.dumps([]))

    # ── Relationships ─────────────────────────────────────────────────────────
    scenario   = relationship("Scenario",        back_populates="plays")
    warehouses = relationship("PlacedWarehouse", back_populates="play")
    quarters   = relationship("QuarterResult",   back_populates="play")


# ─────────────────────────────────────────────────────────────────────────────
#  PLACED WAREHOUSE
# ─────────────────────────────────────────────────────────────────────────────
class PlacedWarehouse(Base):
    __tablename__ = "placed_warehouses"

    id               = Column(Integer, primary_key=True, index=True)
    play_id_fk       = Column(Integer, ForeignKey("plays.id"), nullable=False)
    slot_id          = Column(String(20), nullable=False)   # matches warehouse_slots[].id
    warehouse_type   = Column(String(10), nullable=False)   # small / medium / large
    built_at_quarter = Column(Integer, nullable=False)
    is_active        = Column(Boolean, default=False)       # False while still building
    is_sold          = Column(Boolean, default=False)

    # [{type: "truck"|"drone", purchased_at: Q, is_sold: false}, ...]
    vehicles = Column(Text, default=json.dumps([]))

    play = relationship("Play", back_populates="warehouses")


# ─────────────────────────────────────────────────────────────────────────────
#  QUARTER RESULT  (source of truth; also mirrored into Play.quarterly_results)
# ─────────────────────────────────────────────────────────────────────────────
class QuarterResult(Base):
    __tablename__ = "quarter_results"

    id         = Column(Integer, primary_key=True, index=True)
    play_id_fk = Column(Integer, ForeignKey("plays.id"), nullable=False)
    quarter    = Column(Integer, nullable=False)

    # Snapshot of demand that quarter (for replay / review)
    demand_snapshot = Column(Text, default=json.dumps([]))

    # Financials
    revenue             = Column(Float, default=0.0)
    urgent_revenue      = Column(Float, default=0.0)
    nonurgent_revenue   = Column(Float, default=0.0)
    operating_cost      = Column(Float, default=0.0)
    profit              = Column(Float, default=0.0)
    cash_after          = Column(Float, default=0.0)

    # KPIs
    orders_fulfilled    = Column(Integer, default=0)
    orders_total        = Column(Integer, default=0)
    utilization_rate    = Column(Float,   default=0.0)  # 0–1
    drone_utilization   = Column(Float,   default=0.0)  # 0–1
    truck_utilization   = Column(Float,   default=0.0)  # 0–1
    serving_pct         = Column(Float,   default=0.0)  # 0–1
    stockouts           = Column(Integer, default=0)
    urgent_stockouts    = Column(Integer, default=0)
    nonurgent_stockouts = Column(Integer, default=0)

    # Per-warehouse-type utilization (None = that type not used this quarter)
    small_utilization   = Column(Float, nullable=True, default=None)
    medium_utilization  = Column(Float, nullable=True, default=None)
    large_utilization   = Column(Float, nullable=True, default=None)

    # Per-vehicle-type operating cost breakdown
    drone_cost          = Column(Float, default=0.0)
    truck_cost          = Column(Float, default=0.0)

    play = relationship("Play", back_populates="quarters")
