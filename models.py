"""
models.py — All database tables for Supply Rush
"""

import json
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base
from game_logic import DEFAULT_DISRUPTION_CONFIG

# Single source of truth for default warehouse/vehicle economics — imported
# by routes.py's Normal Mode self-heal so it can never drift from the
# Column defaults declared below.
DEFAULT_WAREHOUSE_CONFIG = {
    "small":  {"purchase_cost": 250_000, "capacity": 500,  "build_quarters": 1, "sell_back": 125_000, "overhead_cost": 20_000},
    "medium": {"purchase_cost": 500_000, "capacity": 1200, "build_quarters": 2, "sell_back": 250_000, "overhead_cost": 30_000},
    "large":  {"purchase_cost": 800_000, "capacity": 2500, "build_quarters": 4, "sell_back": 400_000, "overhead_cost": 40_000},
}
DEFAULT_VEHICLE_CONFIG = {
    "truck": {"purchase_cost": 20_000,  "operating_cost": 800, "capacity": 200, "sell_back": 12_000,  "serves_urgent": False, "serves_nonurgent": True},
    "drone": {"purchase_cost": 9_000,   "operating_cost": 800, "capacity": 60,  "sell_back": 5_400,   "serves_urgent": True,  "serves_nonurgent": True},
}


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
    warehouse_config = Column(Text, default=lambda: json.dumps(DEFAULT_WAREHOUSE_CONFIG))

    # ── Vehicle costs & specs ─────────────────────────────────────────────────
    vehicle_config = Column(Text, default=lambda: json.dumps(DEFAULT_VEHICLE_CONFIG))

    # ── Order pricing ─────────────────────────────────────────────────────────
    urgent_order_revenue    = Column(Float, default=55.0)
    nonurgent_order_revenue = Column(Float, default=25.0)

    # ── Demand parameters ─────────────────────────────────────────────────────
    demand_min_per_zone  = Column(Integer, default=133)
    demand_max_per_zone  = Column(Integer, default=532)
    urgent_demand_ratio  = Column(Float,   default=0.3)

    # Fixed demand zone reveal system (see game_logic.ZONE_SELECTION_RATIO /
    # INITIAL_REVEAL_RATIO for the actual tunable constants): currently 60%
    # of demand_zone_positions is randomly selected per play; 50% of that
    # selection is revealed at Quarter 0, the rest reveal evenly starting at
    # this configurable quarter through the end of the game.
    demand_reveal_start_quarter = Column(Integer, default=6)

    # Max distance (in the same 0-100 x/y coordinate space as all map
    # positions) between a demand zone and a warehouse for that warehouse to
    # serve it — controls route lines, delivery animation, and the
    # capacity/demand label. Instructor-configured only; not shown/adjustable
    # in the student game.
    warehouse_service_radius = Column(Integer, default=30)

    # ── Sell permissions ──────────────────────────────────────────────────────
    allow_sell_warehouses = Column(Boolean, default=True, nullable=False)
    allow_sell_trucks     = Column(Boolean, default=True, nullable=False)
    allow_sell_drones     = Column(Boolean, default=True, nullable=False)

    # ── Outsourcing config ────────────────────────────────────────────────────
    allow_outsourcing        = Column(Boolean, default=False, nullable=False)
    outsource_cost_urgent    = Column(Float, default=75.0, nullable=False)
    outsource_cost_nonurgent = Column(Float, default=40.0, nullable=False)
    allow_moving_vehicles    = Column(Boolean, default=False, nullable=False)

    # ── Quarter gating (instructor-controlled pacing) ─────────────────────────
    # 0 = gating disabled, every play under this scenario code advances freely.
    # N >= 1 = students may run/start any quarter up to and including N; the
    # instructor raises N (via the dashboard) to release the next quarter for
    # every group sharing this scenario code at once.
    unlocked_quarter = Column(Integer, default=0, nullable=False)

    # ── Map layout ────────────────────────────────────────────────────────────
    warehouse_slots       = Column(Text, default=json.dumps([]))  # [{id, x, y}, ...]
    demand_zone_positions = Column(Text, default=json.dumps([]))  # [{id, x, y}, ...]

    # ── Random disruption events ──────────────────────────────────────────────
    # Off by default — see game_logic.roll_disruption_events /
    # DEFAULT_DISRUPTION_CONFIG for the shape and the built-in event types.
    disruption_config = Column(Text, default=json.dumps(DEFAULT_DISRUPTION_CONFIG))

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
    # The verified email of whoever started this play (the "owner"). Set once
    # at creation from require_student_email's dependency value — "unverified
    # @local" when REQUIRE_STUDENT_VERIFICATION is off, matching the same
    # placeholder PlayMember rows already use in that case. A play with
    # multiple students still has exactly one owner here; every joined
    # student's own email lives in PlayMember (see below), which this column
    # does not replace.
    email           = Column(String(255), nullable=True, index=True)
    # Free-text group identifier a student enters alongside the scenario code.
    # Every student who enters the same (scenario_id, group_name) pair (case-
    # insensitive match) lands in this same shared play — see PlayMember for
    # the verified-email membership list. Null for normal mode (no groups).
    group_name      = Column(String(120), nullable=True, index=True)
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
    outsourced_zones       = Column(Text, default=json.dumps([]))

    # Capital spend during Q0 setup (warehouses/vehicles bought before the
    # game's first real quarter, settled at the Q0->Q1 "Start Game" advance
    # call). Q0 never gets its own QuarterResult row, so this is the only
    # durable record of that spend — needed so the investment chart's "Start"
    # bucket survives a page refresh/reconnect instead of resetting to 0.
    setup_capex_warehouses = Column(Float, default=0.0, nullable=False)
    setup_capex_vehicles   = Column(Float, default=0.0, nullable=False)

    # Bundled quarterly results for fast reads — kept in sync with QuarterResult table
    # Format: [ { quarter, revenue, operating_cost, profit, cash_after,
    #             orders_fulfilled, orders_total, utilization_rate, serving_pct, stockouts }, ... ]
    quarterly_results = Column(Text, default=json.dumps([]))

    # ── Relationships ─────────────────────────────────────────────────────────
    scenario   = relationship("Scenario",        back_populates="plays")
    warehouses = relationship("PlacedWarehouse", back_populates="play")
    quarters   = relationship("QuarterResult",   back_populates="play")
    members    = relationship("PlayMember",      back_populates="play")


# ─────────────────────────────────────────────────────────────────────────────
#  PLACED WAREHOUSE
# ─────────────────────────────────────────────────────────────────────────────
class PlacedWarehouse(Base):
    __tablename__ = "placed_warehouses"

    id                  = Column(Integer, primary_key=True, index=True)
    play_id_fk          = Column(Integer, ForeignKey("plays.id"), nullable=False)
    slot_id             = Column(String(20), nullable=False)   # matches warehouse_slots[].id
    warehouse_type      = Column(String(10), nullable=False)   # small / medium / large
    built_at_quarter    = Column(Integer, nullable=False)
    is_active           = Column(Boolean, default=False)       # False while still building
    is_sold             = Column(Boolean, default=False)
    purchase_price_paid = Column(Float, nullable=True)          # cash actually deducted at purchase time

    # [{type: "truck"|"drone", purchased_at: Q, is_sold: false}, ...]
    vehicles = Column(Text, default=json.dumps([]))

    play = relationship("Play", back_populates="warehouses")


# ─────────────────────────────────────────────────────────────────────────────
#  QUARTER RESULT  (source of truth; also mirrored into Play.quarterly_results)
# ─────────────────────────────────────────────────────────────────────────────
class QuarterResult(Base):
    __tablename__ = "quarter_results"
    __table_args__ = (
        UniqueConstraint("play_id_fk", "quarter", name="uq_quarter_results_play_quarter"),
    )

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
    outsource_expenses  = Column(Float, default=0.0)
    outsource_revenue   = Column(Float, default=0.0)

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

    # Warehouse overhead cost breakdown (fixed cost per active warehouse per quarter)
    overhead_cost       = Column(Float, default=0.0)

    # Random disruption events that fired THIS quarter and actually affected
    # this play (see game_logic.roll_disruption_events / events_affecting_play).
    # Persisted so a later instructor edit to Scenario.disruption_config never
    # rewrites what already happened. Format: [{key, kind, severity, message}, ...]
    disruption_events   = Column(Text, default=json.dumps([]))

    play = relationship("Play", back_populates="quarters")


# ─────────────────────────────────────────────────────────────────────────────
#  PLAY MEMBER  (verified students who have joined a shared group play)
# ─────────────────────────────────────────────────────────────────────────────
class PlayMember(Base):
    __tablename__ = "play_members"

    id         = Column(Integer, primary_key=True, index=True)
    play_id_fk = Column(Integer, ForeignKey("plays.id"), nullable=False, index=True)
    email      = Column(String(255), nullable=False, index=True)
    joined_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    play = relationship("Play", back_populates="members")

    __table_args__ = (UniqueConstraint("play_id_fk", "email", name="uq_play_member_email"),)


# ─────────────────────────────────────────────────────────────────────────────
#  VERIFICATION CODE  (passwordless email login — one-time 6-digit codes)
# ─────────────────────────────────────────────────────────────────────────────
class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id                 = Column(Integer, primary_key=True, index=True)
    email              = Column(String(255), nullable=False, index=True)
    code_hash          = Column(String(64),  nullable=False)
    created_at         = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at         = Column(DateTime, nullable=False)
    attempts_remaining = Column(Integer, default=5, nullable=False)
    consumed           = Column(Boolean, default=False, nullable=False)


# ─────────────────────────────────────────────────────────────────────────────
#  APP SECRET  (DB-backed, not per-process — so a random fallback generated
#  when SESSION_SECRET/INSTRUCTOR_PASSWORDS is left unset is identical across
#  every gunicorn worker instead of each worker minting its own value)
# ─────────────────────────────────────────────────────────────────────────────
class AppSecret(Base):
    __tablename__ = "app_secrets"

    id    = Column(Integer, primary_key=True, index=True)
    key   = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(String(255), nullable=False)
