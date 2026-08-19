"""
routes.py — All API endpoints for Supply Rush
"""

import os
import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from database import get_db
from models import (
    Scenario, Play, PlacedWarehouse, QuarterResult, PlayMember,
    DEFAULT_WAREHOUSE_CONFIG, DEFAULT_VEHICLE_CONFIG,
)
from schemas import (
    ScenarioCreate, ScenarioUpdate, ScenarioOut,
    PlayCreate, NormalModeStart,
    PlaceWarehouseRequest, AddVehicleRequest,
    SellWarehouseRequest, SellVehicleRequest,
    QuarterResultOut, OutsourceZoneRequest,
    EmailCodeRequest, EmailCodeVerify,
)
from game_logic import (
    generate_code, generate_play_id,
    generate_demand, simulate_quarter,
    advance_warehouse_builds, quarters_until_active,
    count_vehicles, compute_pending_vehicle_net_cost, compute_vehicle_capex, get_assigned_vehicle_capacity,
    select_and_schedule_demand_zones, get_revealed_zones,
    roll_disruption_events, compute_disruption_modifiers, events_affecting_play,
    DEFAULT_WAREHOUSE_SLOTS, DEFAULT_DEMAND_POSITIONS,
)
from auth import (
    authenticate_instructor, require_student_email, verification_required,
    set_session_cookie, clear_session_cookie, get_session_email,
    is_allowed_email, allowed_email_domain,
    create_verification_code, verify_code,
)

router = APIRouter()
DEFAULT_SCENARIO_CODE = "DEFAULT"
try:
    MAX_GROUP_SIZE = int(os.getenv("MAX_GROUP_SIZE", "60"))
except (TypeError, ValueError):
    MAX_GROUP_SIZE = 60


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_scenario_or_404(code: str, db: Session) -> Scenario:
    sc = db.query(Scenario).filter(Scenario.code == code).first()
    if not sc:
        raise HTTPException(404, f"Scenario '{code}' not found")
    return sc


def _get_play_or_404(play_id: str, db: Session, for_update: bool = False) -> Play:
    """
    for_update=True locks the row for the rest of this transaction — used by
    every endpoint that reads-then-writes play.cash or inserts a PlacedWarehouse
    tied to a slot, so two group members acting at the same moment (a real
    scenario now that a play can be shared by a whole group) serialize instead
    of racing past a lost cash update or a duplicate warehouse on one slot.
    No-op on SQLite (no row-level locking there, and SQLite is single-writer
    anyway); matters under multi-worker Postgres deployments.
    """
    q = db.query(Play).filter(Play.play_id == play_id)
    if for_update:
        q = q.with_for_update()
    play = q.first()
    if not play:
        raise HTTPException(404, "Play not found")
    return play


def _is_member(db: Session, play: Play, email: str) -> bool:
    return db.query(PlayMember).filter(
        PlayMember.play_id_fk == play.id, PlayMember.email == email
    ).first() is not None


def _ensure_member(db: Session, play: Play, email: str) -> None:
    """
    Adds `email` to this play's group membership if not already a member,
    enforcing MAX_GROUP_SIZE. No-op entirely when student verification is
    off, so membership tracking never blocks anything in local dev.
    """
    if not verification_required():
        return
    if _is_member(db, play, email):
        return
    # Lock the play row for the rest of this transaction so concurrent joins
    # against the same play serialize instead of racing past MAX_GROUP_SIZE.
    _get_play_or_404(play.play_id, db, for_update=True)
    if _is_member(db, play, email):
        return
    count = db.query(PlayMember).filter(PlayMember.play_id_fk == play.id).count()
    if count >= MAX_GROUP_SIZE:
        db.rollback()
        raise HTTPException(400, f"This group is full (max {MAX_GROUP_SIZE} students).")
    db.add(PlayMember(play_id_fk=play.id, email=email))
    db.commit()


def _require_member(db: Session, play: Play, email: str) -> None:
    """Rejects the request unless `email` already belongs to this play's group."""
    if not verification_required():
        return
    if not _is_member(db, play, email):
        raise HTTPException(403, "You're not a member of this play.")


def _parse_scenario(sc: Scenario) -> ScenarioOut:
    return ScenarioOut.from_orm_parse(sc)


@router.get("/debug-credentials")
def debug_credentials(db: Session = Depends(get_db)):
    import os
    from models import AppSecret
    secret_row = db.query(AppSecret).filter(AppSecret.key == "instructor_fallback_password").first()
    db_val = secret_row.value if secret_row else None
    return {
        "INSTRUCTOR_USERNAMES": os.getenv("INSTRUCTOR_USERNAMES"),
        "INSTRUCTOR_PASSWORDS": os.getenv("INSTRUCTOR_PASSWORDS"),
        "fallback_password": db_val
    }


def _play_response(play: Play, sc: Scenario) -> dict:
    v_cfg = json.loads(sc.vehicle_config or "{}") if sc else {}
    pending_deltas = json.loads(play.pending_vehicle_deltas or "{}")
    return {
        "play_id":         play.play_id,
        "scenario_code":   play.scenario_code,
        "student_name":    play.student_name,
        "started_at":      play.started_at,
        "current_quarter": play.current_quarter,
        "total_quarters":  sc.total_quarters if sc else 16,
        "cash":            play.cash,
        "pending_vehicle_net_cost": compute_pending_vehicle_net_cost(
            pending_deltas, v_cfg, allow_moving_vehicles=getattr(sc, "allow_moving_vehicles", False) if sc else False
        ),
        "completed":       play.completed,
        "quarterly_results": json.loads(play.quarterly_results or "[]"),
        "outsourced_zones":  json.loads(play.outsourced_zones or "[]"),
        "start_capex": {
            "warehouses": getattr(play, "setup_capex_warehouses", 0.0) or 0.0,
            "vehicles":   getattr(play, "setup_capex_vehicles", 0.0) or 0.0,
        },
        "scenario":        _parse_scenario(sc) if sc else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH — passwordless student email verification
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/auth/request-code", tags=["auth"])
def request_code(payload: EmailCodeRequest, db: Session = Depends(get_db)):
    """Sends a 6-digit verification code to a school email address."""
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Please enter a valid email address.")
    if not is_allowed_email(email):
        raise HTTPException(400, f"Only @{allowed_email_domain()} email addresses are accepted.")
    create_verification_code(db, email)
    return {"detail": "Verification code sent.", "email": email}


@router.post("/auth/verify-code", tags=["auth"])
def verify_code_endpoint(payload: EmailCodeVerify, response: Response, db: Session = Depends(get_db)):
    """Checks the code and, on success, sets a signed session cookie."""
    email = payload.email.strip().lower()
    if not verify_code(db, email, payload.code.strip()):
        raise HTTPException(400, "Invalid or expired code.")
    set_session_cookie(response, email)
    return {"email": email}


@router.get("/auth/me", tags=["auth"])
def auth_me(request: Request):
    """Lets the frontend check verification status without triggering a 401."""
    return {"email": get_session_email(request), "verification_required": verification_required()}


@router.post("/auth/logout", tags=["auth"])
def logout(response: Response):
    clear_session_cookie(response)
    return {"detail": "Logged out"}


# ─────────────────────────────────────────────────────────────────────────────
#  INSTRUCTOR — Scenario management
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/scenarios", response_model=ScenarioOut, tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def create_scenario(payload: ScenarioCreate, db: Session = Depends(get_db)):
    """Instructor creates a scenario. Returns the unique code to share with students."""
    code = generate_code()
    while db.query(Scenario).filter(Scenario.code == code).first():
        code = generate_code()

    wh_slots = [s.dict() for s in payload.warehouse_slots]
    if not wh_slots:
        wh_slots = DEFAULT_WAREHOUSE_SLOTS

    demand_zones = [s.dict() for s in payload.demand_zone_positions]
    if not demand_zones:
        demand_zones = DEFAULT_DEMAND_POSITIONS

    sc = Scenario(
        code=code,
        name=payload.name,
        total_quarters=payload.total_quarters,
        starting_budget=payload.starting_budget,
        warehouse_config=json.dumps({k: v.dict() for k, v in payload.warehouse_config.items()}),
        vehicle_config=json.dumps({k: v.dict() for k, v in payload.vehicle_config.items()}),
        urgent_order_revenue=payload.urgent_order_revenue,
        nonurgent_order_revenue=payload.nonurgent_order_revenue,
        demand_min_per_zone=payload.demand_min_per_zone,
        demand_max_per_zone=payload.demand_max_per_zone,
        urgent_demand_ratio=payload.urgent_demand_ratio,
        demand_reveal_start_quarter=payload.demand_reveal_start_quarter,
        warehouse_service_radius=payload.warehouse_service_radius,
        allow_sell_warehouses=payload.allow_sell_warehouses,
        allow_sell_trucks=payload.allow_sell_trucks,
        allow_sell_drones=payload.allow_sell_drones,
        allow_outsourcing=payload.allow_outsourcing,
        outsource_cost_urgent=payload.outsource_cost_urgent,
        outsource_cost_nonurgent=payload.outsource_cost_nonurgent,
        allow_moving_vehicles=payload.allow_moving_vehicles,
        unlocked_quarter=payload.unlocked_quarter,
        warehouse_slots=json.dumps(wh_slots),
        demand_zone_positions=json.dumps(demand_zones),
        disruption_config=json.dumps(payload.disruption_config.dict()),
    )
    db.add(sc)
    db.commit()
    db.refresh(sc)
    return _parse_scenario(sc)


@router.get("/scenarios", response_model=List[ScenarioOut], tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def list_scenarios(db: Session = Depends(get_db)):
    """Instructor view: list all saved scenarios."""
    scenarios = db.query(Scenario).all()
    return [_parse_scenario(sc) for sc in scenarios]


@router.get("/scenarios/{code}", response_model=ScenarioOut, tags=["instructor"])
def get_scenario(code: str, db: Session = Depends(get_db)):
    # Deliberately NOT instructor-gated: students already receive this same
    # data via _play_response() once they know a scenario code, and the
    # quarter-gating status poll (game.html refreshGateStatus) depends on
    # this endpoint staying reachable without credentials. Listing ALL codes
    # (list_scenarios above) is the sensitive operation, not looking one up.
    return _parse_scenario(_get_scenario_or_404(code, db))


@router.put("/scenarios/{code}", response_model=ScenarioOut, tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def update_scenario(code: str, payload: ScenarioUpdate, db: Session = Depends(get_db)):
    sc = _get_scenario_or_404(code, db)
    if payload.name            is not None: sc.name            = payload.name
    if payload.total_quarters  is not None: sc.total_quarters  = payload.total_quarters
    if payload.starting_budget is not None: sc.starting_budget = payload.starting_budget
    if payload.warehouse_config is not None:
        sc.warehouse_config = json.dumps({k: v.dict() for k, v in payload.warehouse_config.items()})
    if payload.vehicle_config is not None:
        sc.vehicle_config = json.dumps({k: v.dict() for k, v in payload.vehicle_config.items()})
    if payload.urgent_order_revenue    is not None: sc.urgent_order_revenue    = payload.urgent_order_revenue
    if payload.nonurgent_order_revenue is not None: sc.nonurgent_order_revenue = payload.nonurgent_order_revenue
    if payload.demand_min_per_zone     is not None: sc.demand_min_per_zone     = payload.demand_min_per_zone
    if payload.demand_max_per_zone     is not None: sc.demand_max_per_zone     = payload.demand_max_per_zone
    if payload.urgent_demand_ratio     is not None: sc.urgent_demand_ratio     = payload.urgent_demand_ratio
    if payload.demand_reveal_start_quarter is not None: sc.demand_reveal_start_quarter = payload.demand_reveal_start_quarter
    if payload.warehouse_service_radius is not None: sc.warehouse_service_radius = payload.warehouse_service_radius
    if payload.allow_sell_warehouses is not None: sc.allow_sell_warehouses = payload.allow_sell_warehouses
    if payload.allow_sell_trucks     is not None: sc.allow_sell_trucks     = payload.allow_sell_trucks
    if payload.allow_sell_drones     is not None: sc.allow_sell_drones     = payload.allow_sell_drones
    if payload.allow_moving_vehicles is not None: sc.allow_moving_vehicles = payload.allow_moving_vehicles
    if payload.unlocked_quarter is not None: sc.unlocked_quarter = payload.unlocked_quarter
    if payload.warehouse_slots is not None:
        wh_slots = [s.dict() for s in payload.warehouse_slots]
        if not wh_slots:
            wh_slots = DEFAULT_WAREHOUSE_SLOTS
        sc.warehouse_slots = json.dumps(wh_slots)

    if payload.demand_zone_positions is not None:
        demand_zones = [s.dict() for s in payload.demand_zone_positions]
        if not demand_zones:
            demand_zones = DEFAULT_DEMAND_POSITIONS
        sc.demand_zone_positions = json.dumps(demand_zones)
    if payload.disruption_config is not None:
        sc.disruption_config = json.dumps(payload.disruption_config.dict())

    # Only enforce this when the request actually touched one of the two
    # fields — otherwise an unrelated PUT (e.g. just toggling unlocked_quarter)
    # against a pre-existing scenario row would 400 forever if it happens to
    # already be in this state, with no dashboard control able to fix it.
    demand_fields_touched = payload.demand_min_per_zone is not None or payload.demand_max_per_zone is not None
    if demand_fields_touched and sc.demand_max_per_zone < sc.demand_min_per_zone:
        raise HTTPException(400, "demand_max_per_zone must be >= demand_min_per_zone")

    db.commit()
    db.refresh(sc)
    return _parse_scenario(sc)


@router.delete("/scenarios/{code}", tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def delete_scenario(code: str, db: Session = Depends(get_db)):
    sc = _get_scenario_or_404(code, db)
    plays = db.query(Play).filter(Play.scenario_id == sc.id).all()
    play_ids = [p.id for p in plays]
    if play_ids:
        db.query(QuarterResult).filter(QuarterResult.play_id_fk.in_(play_ids)).delete(synchronize_session=False)
        db.query(PlacedWarehouse).filter(PlacedWarehouse.play_id_fk.in_(play_ids)).delete(synchronize_session=False)
        db.query(PlayMember).filter(PlayMember.play_id_fk.in_(play_ids)).delete(synchronize_session=False)
        db.query(Play).filter(Play.id.in_(play_ids)).delete(synchronize_session=False)
    db.delete(sc)
    db.commit()
    return {"detail": f"Scenario '{code}' deleted"}


# ─────────────────────────────────────────────────────────────────────────────
#  INSTRUCTOR — Plays view (ALL plays across ALL scenarios)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/plays", tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def list_all_plays(
    db: Session = Depends(get_db),
    search: Optional[str] = Query(None, description="Filter by student name or scenario code"),
    scenario_code: Optional[str] = Query(None),
    completed: Optional[bool] = Query(None),
):
    """
    Instructor view: all plays across all scenarios.
    Supports search by student name or scenario code, and filters by completed status.
    """
    q = db.query(Play).options(selectinload(Play.members), selectinload(Play.scenario))

    if scenario_code:
        q = q.filter(Play.scenario_code == scenario_code.upper())
    if completed is not None:
        q = q.filter(Play.completed == completed)
    if search:
        term = f"%{search}%"
        q = q.filter(
            Play.student_name.ilike(term) |
            Play.play_id.ilike(term) |
            Play.scenario_code.ilike(term)
        )

    plays = q.order_by(Play.started_at.desc()).all()

    return [
        {
            "play_id":         p.play_id,
            "scenario_code":   p.scenario_code,
            "student_name":    p.student_name,
            "group_name":      p.group_name,
            "member_emails":   [m.email for m in p.members],
            "started_at":      p.started_at,
            "current_quarter": p.current_quarter,
            "total_quarters":  p.scenario.total_quarters if p.scenario else 16,
            "cash":            p.cash,
            "completed":       p.completed,
            "quarterly_results": json.loads(p.quarterly_results or "[]"),
        }
        for p in plays
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT — Start a play
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/sessions", tags=["student"])
def start_play(payload: PlayCreate, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    """
    Student enters a scenario code (+ group name, required once student
    verification is on) → joins the group's existing play if one already
    exists for this scenario, otherwise creates one. This is the ONLY
    mechanism for multiple students to land in the same shared play — there
    is no separate join-by-link flow, so every group member calls this same
    endpoint with the same (scenario_code, group_name) pair.
    """
    sc = _get_scenario_or_404(payload.scenario_code.upper(), db)
    group_name = (payload.group_name or "").strip()

    if verification_required() and not group_name:
        raise HTTPException(400, "Please enter a group name.")

    play = None
    if group_name:
        play = (
            db.query(Play)
            .filter(Play.scenario_id == sc.id, Play.completed == False)
            .filter(func.lower(Play.group_name) == group_name.lower())
            .first()
        )

    if play:
        _ensure_member(db, play, email)
        return _play_response(play, sc)

    pid = generate_play_id()
    master_zones = json.loads(sc.demand_zone_positions or "[]")
    schedule = select_and_schedule_demand_zones(
        master_zones, sc.total_quarters, sc.demand_reveal_start_quarter
    )
    play = Play(
        play_id=pid,
        scenario_code=sc.code,
        scenario_id=sc.id,
        student_name=payload.student_name,
        group_name=group_name or None,
        current_quarter=0,   # Quarter 0 = setup phase, no demand/simulation
        cash=sc.starting_budget,
        quarterly_results=json.dumps([]),
        demand_zone_schedule=json.dumps(schedule),
        activated_demand_zones=json.dumps({"urgent": [], "nonurgent": []}),
        pending_vehicle_deltas=json.dumps({}),
    )
    db.add(play)
    try:
        db.commit()
    except IntegrityError:
        # Another request for the same (scenario, group_name) committed first
        # (the partial unique index on plays closes this race) — join the
        # play that won instead of failing the request.
        db.rollback()
        play = (
            db.query(Play)
            .filter(Play.scenario_id == sc.id, Play.completed == False)
            .filter(func.lower(Play.group_name) == group_name.lower())
            .first()
        )
        if not play:
            raise
        _ensure_member(db, play, email)
        return _play_response(play, sc)
    db.refresh(play)
    _ensure_member(db, play, email)
    return _play_response(play, sc)


@router.post("/sessions/normal", tags=["student"])
def start_normal_play(payload: NormalModeStart, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    """Normal mode: no code, no groups — always a fresh solo play. Uses DEFAULT scenario (auto-created with real positions if missing)."""
    sc = db.query(Scenario).filter(Scenario.code == DEFAULT_SCENARIO_CODE).first()
    if not sc:
        sc = Scenario(
            code=DEFAULT_SCENARIO_CODE,
            name="Default Scenario",
            warehouse_slots=json.dumps(DEFAULT_WAREHOUSE_SLOTS),
            demand_zone_positions=json.dumps(DEFAULT_DEMAND_POSITIONS),
            allow_outsourcing=True,
        )
        db.add(sc)
        db.commit()
        db.refresh(sc)
    # If DEFAULT exists but has empty slots (legacy), different slot counts, or outdated coordinates, update/backfill
    try:
        existing_slots = json.loads(sc.warehouse_slots or "[]")
    except Exception:
        existing_slots = []

    try:
        existing_demand = json.loads(sc.demand_zone_positions or "[]")
    except Exception:
        existing_demand = []

    # Only backfill truly-empty legacy rows here. Overwriting whenever the
    # constants merely differ would rewrite slot ids out from under any
    # in-progress play (a placed warehouse's slot_id would stop resolving
    # in advance_quarter's coordinate lookup, silently zeroing its position).
    dirty = False
    if not existing_slots:
        sc.warehouse_slots = json.dumps(DEFAULT_WAREHOUSE_SLOTS)
        dirty = True

    # Demand zones are safe to backfill additively (unlike slots, above) —
    # DEFAULT_DEMAND_POSITIONS only ever grows by appending new ids with new
    # coordinates; existing ids keep their original x/y forever, so merging
    # in newly-added ids can't shift what an in-progress play's already-
    # scheduled zone_ids point to.
    existing_ids = {z["id"] for z in existing_demand}
    missing = [z for z in DEFAULT_DEMAND_POSITIONS if z["id"] not in existing_ids]
    if missing:
        existing_demand = existing_demand + missing
        sc.demand_zone_positions = json.dumps(existing_demand)
        dirty = True

    if dirty:
        db.commit()
        db.refresh(sc)

    # Compare configs safely as deserialized dicts and self-heal legacy settings
    current_wh_cfg = json.loads(sc.warehouse_config or "{}")
    current_v_cfg = json.loads(sc.vehicle_config or "{}")

    target_wh_cfg = DEFAULT_WAREHOUSE_CONFIG
    target_v_cfg = DEFAULT_VEHICLE_CONFIG

    if current_wh_cfg != target_wh_cfg or current_v_cfg != target_v_cfg or not sc.allow_outsourcing:
        sc.warehouse_config = json.dumps(target_wh_cfg)
        sc.vehicle_config = json.dumps(target_v_cfg)
        sc.allow_outsourcing = True
        db.commit()
        db.refresh(sc)

    pid = generate_play_id()
    master_zones = json.loads(sc.demand_zone_positions or "[]")
    schedule = select_and_schedule_demand_zones(
        master_zones, sc.total_quarters, sc.demand_reveal_start_quarter
    )
    play = Play(
        play_id=pid,
        scenario_code=None,   # null = normal mode
        scenario_id=sc.id,
        student_name=payload.student_name,
        current_quarter=0,   # Quarter 0 = setup phase, no demand/simulation
        cash=sc.starting_budget,
        quarterly_results=json.dumps([]),
        demand_zone_schedule=json.dumps(schedule),
        activated_demand_zones=json.dumps({"urgent": [], "nonurgent": []}),
        pending_vehicle_deltas=json.dumps({}),
    )
    db.add(play)
    db.commit()
    db.refresh(play)
    _ensure_member(db, play, email)
    return _play_response(play, sc)


@router.get("/sessions/{play_id}", tags=["student"])
def get_play(play_id: str, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    """Fetch current play state (for page refresh / reconnect)."""
    play = _get_play_or_404(play_id, db)
    _require_member(db, play, email)
    return _play_response(play, play.scenario)


@router.post("/sessions/{play_id}/outsource", tags=["student"])
def toggle_outsource_zone(play_id: str, payload: OutsourceZoneRequest, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    play = _get_play_or_404(play_id, db, for_update=True)
    _require_member(db, play, email)
    if play.completed:
        raise HTTPException(400, "Game is already complete")
    sc = play.scenario
    if not getattr(sc, "allow_outsourcing", False):
        raise HTTPException(400, "Outsourcing is disabled in this scenario")
        
    outsourced = json.loads(play.outsourced_zones or "[]")
    if payload.outsourced:
        if payload.zone_id not in outsourced:
            outsourced.append(payload.zone_id)
    else:
        if payload.zone_id in outsourced:
            outsourced.remove(payload.zone_id)
            
    play.outsourced_zones = json.dumps(outsourced)
    db.commit()
    return {
        "outsourced_zones": outsourced,
        "cash_remaining": play.cash,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT — Warehouse & vehicle actions
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/sessions/{play_id}/warehouses", tags=["student"])
def place_warehouse(play_id: str, payload: PlaceWarehouseRequest, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    play = _get_play_or_404(play_id, db, for_update=True)
    _require_member(db, play, email)
    if play.completed:
        raise HTTPException(400, "Game is already complete")

    sc = play.scenario
    wh_cfg = json.loads(sc.warehouse_config or "{}")
    cfg = wh_cfg.get(payload.warehouse_type)
    if not cfg:
        raise HTTPException(400, f"Unknown warehouse type: {payload.warehouse_type}")

    valid_slot_ids = {slot["id"] for slot in json.loads(sc.warehouse_slots or "[]")}
    if payload.slot_id not in valid_slot_ids:
        raise HTTPException(400, f"Unknown slot: {payload.slot_id}")

    v_cfg = json.loads(sc.vehicle_config or "{}")
    deltas = json.loads(play.pending_vehicle_deltas or "{}")
    pending_net_cost = compute_pending_vehicle_net_cost(
        deltas, v_cfg, allow_moving_vehicles=getattr(sc, "allow_moving_vehicles", False)
    )
    cost = cfg["purchase_cost"]
    
    if play.cash - pending_net_cost < cost:
        raise HTTPException(
            400,
            f"Insufficient cash: need ${cost:,.0f} (plus ${pending_net_cost:,.0f} pending vehicle costs), "
            f"have ${play.cash:,.0f}."
        )

    existing = db.query(PlacedWarehouse).filter(
        PlacedWarehouse.play_id_fk == play.id,
        PlacedWarehouse.slot_id == payload.slot_id,
        PlacedWarehouse.is_sold == False,
    ).first()
    if existing:
        raise HTTPException(400, f"Slot '{payload.slot_id}' is already occupied")

    play.cash -= cost
    wh = PlacedWarehouse(
        play_id_fk=play.id,
        slot_id=payload.slot_id,
        warehouse_type=payload.warehouse_type,
        built_at_quarter=play.current_quarter,
        is_active=cfg["build_quarters"] == 0,
        purchase_price_paid=cost,
    )
    db.add(wh)
    db.commit()
    db.refresh(wh)
    # "Next run" means: the Q0->Q1 transition if still in setup, otherwise
    # the next real quarter about to be simulated.
    next_run_reference = 1 if play.current_quarter == 0 else play.current_quarter
    active_from = wh.built_at_quarter + cfg["build_quarters"]
    return {
        "warehouse_id":         wh.id,
        "slot_id":              wh.slot_id,
        "warehouse_type":       wh.warehouse_type,
        "built_at_quarter":     wh.built_at_quarter,
        "is_active":            wh.is_active,
        "cash_remaining":       play.cash,
        "active_at_quarter":    play.current_quarter + cfg["build_quarters"],
        "quarters_until_active": cfg["build_quarters"],
        "activates_next_run":   (not wh.is_active) and active_from <= next_run_reference,
        "vehicle_counts":       {},
    }


@router.post("/sessions/{play_id}/vehicles", tags=["student"])
def add_vehicle(play_id: str, payload: AddVehicleRequest, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    play = _get_play_or_404(play_id, db, for_update=True)
    _require_member(db, play, email)
    if play.completed:
        raise HTTPException(400, "Game is already complete")
    wh = db.query(PlacedWarehouse).filter(
        PlacedWarehouse.id == payload.warehouse_id,
        PlacedWarehouse.play_id_fk == play.id,
        PlacedWarehouse.is_sold == False,
    ).first()
    if not wh:
        raise HTTPException(404, "Warehouse not found")

    sc = play.scenario
    v_cfg = json.loads(sc.vehicle_config or "{}")
    cfg = v_cfg.get(payload.vehicle_type)
    if not cfg:
        raise HTTPException(400, f"Unknown vehicle type: {payload.vehicle_type}")

    # A warehouse can only hold as much fleet capacity as its own capacity
    # rating — a small (500/qtr) warehouse can't be stocked with, say, 10
    # trucks (2,000/qtr of capacity) just because the player can afford them.
    wh_cfg = json.loads(sc.warehouse_config or "{}")
    wh_capacity_limit = wh_cfg.get(wh.warehouse_type, {}).get("capacity", 0)
    used_capacity = get_assigned_vehicle_capacity(wh.vehicles, v_cfg)
    if used_capacity + cfg["capacity"] > wh_capacity_limit:
        raise HTTPException(
            400,
            f"This warehouse can only hold {wh_capacity_limit:,.0f}/qtr of vehicle capacity — "
            f"{used_capacity:,.0f}/qtr already assigned, adding this {payload.vehicle_type} "
            f"({cfg['capacity']:,.0f}/qtr) would exceed it.",
        )

    # Vehicle cash is settled at Run Quarter time, netted per type against
    # this quarter's other pending buy/sell actions (see
    # compute_pending_vehicle_net_cost) — buying and selling the same count
    # of a type nets to zero, so relocating between warehouses is free.
    # Affordability is still checked NOW, against what the net cost would be
    # if this purchase proceeds (a prior sell this same quarter already
    # counts toward affordability, exactly like "moving" would).
    deltas = json.loads(play.pending_vehicle_deltas or "{}")
    type_deltas = deltas.get(payload.vehicle_type, {"bought": 0, "sold": 0})
    type_deltas["bought"] += 1
    deltas[payload.vehicle_type] = type_deltas

    projected_net_cost = compute_pending_vehicle_net_cost(
        deltas, v_cfg, allow_moving_vehicles=getattr(sc, "allow_moving_vehicles", False)
    )
    if play.cash - projected_net_cost < 0:
        raise HTTPException(400, f"Insufficient cash: this purchase would need ${projected_net_cost:,.0f} net this quarter, have ${play.cash:,.0f}")

    import uuid
    play.pending_vehicle_deltas = json.dumps(deltas)
    vehicles = json.loads(wh.vehicles or "[]")
    vehicle_id = uuid.uuid4().hex[:8]
    vehicles.append({"id": vehicle_id, "type": payload.vehicle_type, "purchased_at": play.current_quarter, "is_sold": False})
    wh.vehicles = json.dumps(vehicles)
    db.commit()
    return {
        "vehicles": vehicles,
        "cash_remaining": play.cash,  # unchanged — confirmed cash, frozen until Run Quarter
        "pending_vehicle_net_cost": projected_net_cost,
    }


@router.post("/sessions/{play_id}/warehouses/sell", tags=["student"])
def sell_warehouse(play_id: str, payload: SellWarehouseRequest, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    play = _get_play_or_404(play_id, db, for_update=True)
    _require_member(db, play, email)
    if play.completed:
        raise HTTPException(400, "Game is already complete")
    wh = db.query(PlacedWarehouse).filter(
        PlacedWarehouse.id == payload.warehouse_id,
        PlacedWarehouse.play_id_fk == play.id,
        PlacedWarehouse.is_sold == False,
    ).first()
    if not wh:
        raise HTTPException(404, "Warehouse not found")

    sc = play.scenario
    if not getattr(sc, "allow_sell_warehouses", True):
        raise HTTPException(400, "Selling warehouses is disabled in this scenario")

    wh_cfg = json.loads(sc.warehouse_config or "{}")
    sell_price = wh_cfg[wh.warehouse_type]["sell_back"]
    
    # Auto-refund any active vehicles inside the sold warehouse
    v_cfg = json.loads(sc.vehicle_config or "{}")
    vehicles = json.loads(wh.vehicles or "[]")
    deltas = json.loads(play.pending_vehicle_deltas or "{}")
    for v in vehicles:
        if not v.get("is_sold"):
            vtype = v["type"]
            if v.get("purchased_at") == play.current_quarter:
                # Cancel current-quarter purchase
                type_deltas = deltas.get(vtype, {"bought": 0, "sold": 0})
                type_deltas["bought"] = max(0, type_deltas["bought"] - 1)
                deltas[vtype] = type_deltas
            else:
                # Refund immediately (existing vehicle from prior quarters)
                v_sell_price = v_cfg.get(vtype, {}).get("sell_back", 0)
                sell_price += v_sell_price
            v["is_sold"] = True
    wh.vehicles = json.dumps(vehicles)
    play.pending_vehicle_deltas = json.dumps(deltas)

    play.cash += sell_price
    wh.is_sold = True
    db.commit()
    return {"sell_price": sell_price, "cash_remaining": play.cash}


@router.post("/sessions/{play_id}/vehicles/sell", tags=["student"])
def sell_vehicle(play_id: str, payload: SellVehicleRequest, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    play = _get_play_or_404(play_id, db, for_update=True)
    _require_member(db, play, email)
    if play.completed:
        raise HTTPException(400, "Game is already complete")
    wh = db.query(PlacedWarehouse).filter(
        PlacedWarehouse.id == payload.warehouse_id,
        PlacedWarehouse.play_id_fk == play.id,
        PlacedWarehouse.is_sold == False,
    ).first()
    if not wh:
        raise HTTPException(404, "Warehouse not found")

    import uuid
    vehicles = json.loads(wh.vehicles or "[]")
    # Backfill unique IDs for legacy vehicles
    for i, v in enumerate(vehicles):
        if "id" not in v:
            v["id"] = f"legacy_{i}_{uuid.uuid4().hex[:6]}"

    target_vehicle = None
    for v in vehicles:
        if v.get("id") == payload.vehicle_id and not v.get("is_sold"):
            target_vehicle = v
            break

    if not target_vehicle:
        raise HTTPException(400, "Vehicle not found or already sold")

    sc = play.scenario
    v_cfg = json.loads(sc.vehicle_config or "{}")
    vtype = target_vehicle["type"]
    if vtype == "truck" and not getattr(sc, "allow_sell_trucks", True):
        raise HTTPException(400, "Selling trucks is disabled in this scenario")
    if vtype == "drone" and not getattr(sc, "allow_sell_drones", True):
        raise HTTPException(400, "Selling drones is disabled in this scenario")

    deltas = json.loads(play.pending_vehicle_deltas or "{}")
    type_deltas = deltas.get(vtype, {"bought": 0, "sold": 0})
    type_deltas["sold"] += 1
    deltas[vtype] = type_deltas
    play.pending_vehicle_deltas = json.dumps(deltas)

    target_vehicle["is_sold"] = True
    wh.vehicles = json.dumps(vehicles)
    db.commit()

    projected_net_cost = compute_pending_vehicle_net_cost(
        deltas, v_cfg, allow_moving_vehicles=getattr(sc, "allow_moving_vehicles", False)
    )
    return {
        "sell_price": v_cfg[vtype]["sell_back"],
        "cash_remaining": play.cash,  # unchanged — confirmed cash, frozen until Run Quarter
        "pending_vehicle_net_cost": projected_net_cost,
    }


@router.get("/sessions/{play_id}/warehouses", tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def get_warehouses(play_id: str, db: Session = Depends(get_db)):
    """Instructor-only: used by the play-details modal in the dashboard (not called by the student game)."""
    play = _get_play_or_404(play_id, db)
    sc   = play.scenario
    wh_cfg = json.loads(sc.warehouse_config or "{}")
    ref_quarter = 1 if play.current_quarter == 0 else play.current_quarter
    return [
        {
            "id":                  wh.id,
            "slot_id":             wh.slot_id,
            "warehouse_type":      wh.warehouse_type,
            "built_at_quarter":    wh.built_at_quarter,
            "is_active":           wh.is_active,
            "is_sold":             wh.is_sold,
            "vehicles":            json.loads(wh.vehicles or "[]"),
            "vehicle_counts":      count_vehicles(wh.vehicles),
            "quarters_until_active": quarters_until_active(wh, wh_cfg, play.current_quarter),
            "activates_next_run":    (not wh.is_active) and quarters_until_active(wh, wh_cfg, ref_quarter) == 0,
        }
        for wh in play.warehouses if not wh.is_sold
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT — Quarter flow
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/sessions/{play_id}/demand", tags=["student"])
def get_demand(play_id: str, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    """
    Generate demand for the current quarter, restricted to this play's fixed
    zone schedule (zones already revealed as of the current quarter).
    Quarter 0 is setup-only: no demand is generated, only revealed_zones
    (location markers) are returned so the player can plan placement.

    This is a read-only PREVIEW of the upcoming quarter — it does not persist
    newly-activated zones (that happens in /advance). Because generation is
    fully deterministic given the same seed and the same already-activated
    zone set, calling this before /advance for the same quarter always
    matches what /advance will actually compute and commit.
    Frontend uses this to render demand spots and draw nearest-warehouse routes.
    Seeded by (play.id * 100 + quarter) for reproducibility.
    """
    play = _get_play_or_404(play_id, db)
    _require_member(db, play, email)
    sc   = play.scenario
    schedule = json.loads(play.demand_zone_schedule or '[]')
    revealed = get_revealed_zones(schedule, play.current_quarter)

    disruption_events = []
    if play.current_quarter == 0:
        demand = {"urgent": [], "nonurgent": []}
    else:
        activated = json.loads(play.activated_demand_zones or '{"urgent": [], "nonurgent": []}')
        seed = play.id * 100 + play.current_quarter
        demand = generate_demand(
            demand_zone_positions=revealed,
            activated_urgent=activated.get("urgent", []),
            activated_nonurgent=activated.get("nonurgent", []),
            demand_min=sc.demand_min_per_zone,
            demand_max=sc.demand_max_per_zone,
            urgent_ratio=sc.urgent_demand_ratio,
            quarter=play.current_quarter,
            total_quarters=sc.total_quarters,
            seed=seed,
        )
        # Forecast: same roll every play under this scenario code will see for
        # this quarter (seeded off scenario_id, not play.id) — shown ahead of
        # Run Quarter so it's an actionable warning, not a post-hoc surprise.
        all_events = roll_disruption_events(
            json.loads(sc.disruption_config or "{}"), sc.id, play.current_quarter,
            [s["id"] for s in json.loads(sc.warehouse_slots or "[]")],
        )
        disruption_events = [
            {"message": ev["message"], "kind": ev["kind"]}
            for ev in events_affecting_play(all_events, play.warehouses)
        ]
    return {
        "quarter": play.current_quarter,
        "demand_zones": demand,
        "revealed_zones": revealed,
        "disruption_events": disruption_events,
    }


@router.post("/sessions/{play_id}/advance", tags=["student"])
def advance_quarter(play_id: str, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    """Run current quarter simulation, save results, advance to next quarter."""
    play = _get_play_or_404(play_id, db, for_update=True)
    _require_member(db, play, email)
    if play.completed:
        raise HTTPException(400, "Game is already complete")

    sc = play.scenario
    # Instructor-controlled pacing: 0 = gating off. Otherwise the quarter
    # about to be run/started must be <= sc.unlocked_quarter, shared by every
    # play under this scenario code so all groups advance together.
    target_quarter = play.current_quarter if play.current_quarter >= 1 else 1
    if getattr(sc, "unlocked_quarter", 0) and target_quarter > sc.unlocked_quarter:
        raise HTTPException(403, "This quarter hasn't been unlocked by the instructor yet.")

    wh_cfg = json.loads(sc.warehouse_config or "{}")
    v_cfg  = json.loads(sc.vehicle_config or "{}")

    # Settle this quarter's pending vehicle buy/sell actions now, netted per
    # type (see compute_pending_vehicle_net_cost) — this is the moment
    # "Run Quarter" actually charges/credits cash for fleet changes made
    # during planning. Then reset the ledger so nothing carries into the
    # next quarter's planning window.
    pending_deltas = json.loads(play.pending_vehicle_deltas or "{}")
    pending_net_cost = compute_pending_vehicle_net_cost(
        pending_deltas, v_cfg, allow_moving_vehicles=getattr(sc, "allow_moving_vehicles", False)
    )
    play.cash -= pending_net_cost
    play.pending_vehicle_deltas = json.dumps({})

    # Capital investment made during the quarter about to run/start — gross
    # spend on NEW capacity (warehouse builds + net-new vehicle purchases),
    # for the "when did they build capacity" investment chart. Captured
    # before play.current_quarter is mutated below, and before is_sold status
    # changes, so it reflects what was actually spent at the time it happened.
    capex_quarter = play.current_quarter
    capex_vehicles = compute_vehicle_capex(
        pending_deltas, v_cfg, allow_moving_vehicles=getattr(sc, "allow_moving_vehicles", False)
    )
    def _purchase_price(wh):
        return wh.purchase_price_paid if wh.purchase_price_paid is not None \
            else wh_cfg.get(wh.warehouse_type, {}).get("purchase_cost", 0)

    capex_warehouses = sum(
        _purchase_price(wh)
        for wh in play.warehouses
        if wh.built_at_quarter == capex_quarter and not wh.is_sold
    )
    # A warehouse bought AND sold back within this same planning quarter is
    # excluded above (not wh.is_sold), but real cash was still lost on the
    # round trip (purchase price minus sell-back) — count that net loss so
    # the investment chart doesn't silently show $0 for a quarter where the
    # student actually lost capital.
    capex_warehouses += sum(
        max(0, _purchase_price(wh) - wh_cfg.get(wh.warehouse_type, {}).get("sell_back", 0))
        for wh in play.warehouses
        if wh.built_at_quarter == capex_quarter and wh.is_sold
    )

    # ── Quarter 0 (Setup) ────────────────────────────────────────────────
    # Q0 is a real, distinct quarter with demand forced to 0 — players can
    # only place warehouses (and vehicles) against empty slots and revealed-
    # zone markers, nothing is fulfilled or earned. Pressing "Start" (this
    # endpoint, called while current_quarter==0) does exactly two things:
    #   1. Credits every placed warehouse with EXACTLY 1 quarter of build
    #      progress (as if Q0 itself was "run" and 1 quarter elapsed) —
    #      a small (build_quarters=1) warehouse is therefore fully built
    #      and ready the instant the player arrives at Quarter 1, while
    #      medium/large show partial progress (1 quarter credited, still
    #      building).
    #   2. Moves the game to Quarter 1. No results popup, no P&L entry —
    #      there's nothing to show since demand was 0 by design.
    # From Quarter 1 onward the game behaves completely normally: real
    # demand, real simulation, one real quarter fulfilled per click.
    if play.current_quarter == 0:
        advance_warehouse_builds(play.warehouses, 1, wh_cfg)
        play.current_quarter = 1
        play.setup_capex_warehouses = round(capex_warehouses, 2)
        play.setup_capex_vehicles   = round(capex_vehicles, 2)
        db.commit()

        warehouse_states = [
            {
                "id":                    wh.id,
                "slot_id":               wh.slot_id,
                "warehouse_type":        wh.warehouse_type,
                "is_active":             wh.is_active,
                "is_sold":               wh.is_sold,
                "built_at_quarter":      wh.built_at_quarter,
                "quarters_until_active": quarters_until_active(wh, wh_cfg, 1),
                "activates_next_run":    (not wh.is_active) and quarters_until_active(wh, wh_cfg, 1) == 0,
                "vehicle_counts":        count_vehicles(wh.vehicles),
                "vehicles":              json.loads(wh.vehicles or "[]"),
            }
            for wh in play.warehouses if not wh.is_sold
        ]
        return {
            "revenue": 0.0, "operating_cost": 0.0, "profit": 0.0,
            "orders_fulfilled": 0, "orders_total": 0,
            "utilization_rate": 0.0, "serving_pct": 0.0, "stockouts": 0,
            "warehouse_breakdown": [],
            "capex_warehouses": round(capex_warehouses, 2),
            "capex_vehicles":   round(capex_vehicles, 2),
            "quarter":          0,
            "cash_after":       play.cash,
            "pending_vehicle_net_cost": 0.0,
            "completed":        False,
            "next_quarter":     play.current_quarter,
            "warehouse_states": warehouse_states,
        }

    # ── Quarters 1+ (normal play) ────────────────────────────────────────
    ran_quarter = play.current_quarter

    schedule  = json.loads(play.demand_zone_schedule or '[]')
    revealed  = get_revealed_zones(schedule, ran_quarter)
    activated = json.loads(play.activated_demand_zones or '{"urgent": [], "nonurgent": []}')
    seed      = play.id * 100 + ran_quarter

    advance_warehouse_builds(play.warehouses, ran_quarter, wh_cfg)

    demand = generate_demand(
        demand_zone_positions=revealed,
        activated_urgent=activated.get("urgent", []),
        activated_nonurgent=activated.get("nonurgent", []),
        demand_min=sc.demand_min_per_zone,
        demand_max=sc.demand_max_per_zone,
        urgent_ratio=sc.urgent_demand_ratio,
        quarter=ran_quarter,
        total_quarters=sc.total_quarters,
        seed=seed,
    )
    # Persist the (now possibly larger) set of permanently-activated zones
    play.activated_demand_zones = json.dumps({
        "urgent":    demand["activated_urgent"],
        "nonurgent": demand["activated_nonurgent"],
    })

    # Flatten for simulation — urgent and nonurgent are separate spot lists, retaining position info
    outsourced_zones = set(json.loads(play.outsourced_zones or "[]"))
    outsource_expenses = 0.0
    outsource_urgent_rev = 0.0
    outsource_nonurgent_rev = 0.0
    outsource_urgent_fulfilled = 0
    outsource_nonurgent_fulfilled = 0
    outsource_total = 0

    demand_flat = []
    for spot in demand["urgent"]:
        zid = spot["zone_id"]
        orders = spot["orders"]
        if zid in outsourced_zones:
            outsource_urgent_rev += orders * sc.urgent_order_revenue
            outsource_expenses += orders * getattr(sc, "outsource_cost_urgent", 75.0)
            outsource_urgent_fulfilled += orders
            outsource_total += orders
        else:
            demand_flat.append({
                "zone_id":          zid,
                "x":                spot["x"],
                "y":                spot["y"],
                "urgent_orders":    orders,
                "nonurgent_orders": 0,
            })

    for spot in demand["nonurgent"]:
        zid = spot["zone_id"]
        orders = spot["orders"]
        if zid in outsourced_zones:
            outsource_nonurgent_rev += orders * sc.nonurgent_order_revenue
            outsource_expenses += orders * getattr(sc, "outsource_cost_nonurgent", 40.0)
            outsource_nonurgent_fulfilled += orders
            outsource_total += orders
        else:
            demand_flat.append({
                "zone_id":          zid,
                "x":                spot["x"],
                "y":                spot["y"],
                "urgent_orders":    0,
                "nonurgent_orders": orders,
            })

    # Attach coordinates to warehouses from the scenario's warehouse slots
    slots_by_id = {s["id"]: s for s in json.loads(sc.warehouse_slots or "[]")}
    for wh in play.warehouses:
        slot = slots_by_id.get(wh.slot_id)
        if slot:
            wh.x = slot["x"]
            wh.y = slot["y"]
        else:
            wh.x = 0.0
            wh.y = 0.0

    # Same shared-per-scenario roll as the /demand preview above (identical
    # seed inputs) — every play under this scenario code hits the same event
    # on the same quarter; only plays with a warehouse at the targeted slot
    # actually feel a warehouse_capacity event.
    all_disruption_events = roll_disruption_events(
        json.loads(sc.disruption_config or "{}"), sc.id, ran_quarter,
        list(slots_by_id.keys()),
    )
    disruption_modifiers = compute_disruption_modifiers(all_disruption_events, play.warehouses)
    play_disruption_events = events_affecting_play(all_disruption_events, play.warehouses)

    result = simulate_quarter(
        demand_zones=demand_flat,
        warehouses=play.warehouses,
        warehouse_config=wh_cfg,
        vehicle_config=v_cfg,
        urgent_revenue=sc.urgent_order_revenue,
        nonurgent_revenue=sc.nonurgent_order_revenue,
        current_quarter=ran_quarter,
        service_radius=sc.warehouse_service_radius,
        disruption_modifiers=disruption_modifiers,
    )
    result["disruption_events"] = [
        {"message": ev["message"], "kind": ev["kind"]} for ev in play_disruption_events
    ]

    # Combine outsourcing with warehouse simulation results
    outsource_revenue = outsource_urgent_rev + outsource_nonurgent_rev
    outsource_fulfilled = outsource_urgent_fulfilled + outsource_nonurgent_fulfilled
    result["revenue"] = result["revenue"] + outsource_revenue
    result["urgent_revenue"] = result["urgent_revenue"] + outsource_urgent_rev
    result["nonurgent_revenue"] = result["nonurgent_revenue"] + outsource_nonurgent_rev
    result["operating_cost"] = result["operating_cost"] + outsource_expenses
    result["outsource_expenses"] = outsource_expenses
    result["outsource_revenue"] = outsource_revenue
    result["profit"] = result["revenue"] - result["operating_cost"]
    result["orders_fulfilled"] = result["orders_fulfilled"] + outsource_fulfilled
    result["orders_total"] = result["orders_total"] + outsource_total
    result["urgent_fulfilled"] = result["urgent_fulfilled"] + outsource_urgent_fulfilled
    result["nonurgent_fulfilled"] = result["nonurgent_fulfilled"] + outsource_nonurgent_fulfilled
    result["serving_pct"] = round((result["orders_fulfilled"] / result["orders_total"]) if result["orders_total"] > 0 else 0.0, 4)
    result["capex_warehouses"] = round(capex_warehouses, 2)
    result["capex_vehicles"]   = round(capex_vehicles, 2)

    play.cash += result["profit"]

    # Save to QuarterResult table
    wtu = result["warehouse_type_utilization"]
    qr = QuarterResult(
        play_id_fk=play.id,
        quarter=ran_quarter,
        demand_snapshot=json.dumps(demand_flat),
        revenue=result["revenue"],
        urgent_revenue=result["urgent_revenue"],
        nonurgent_revenue=result["nonurgent_revenue"],
        operating_cost=result["operating_cost"],
        profit=result["profit"],
        cash_after=play.cash,
        orders_fulfilled=result["orders_fulfilled"],
        orders_total=result["orders_total"],
        utilization_rate=result["utilization_rate"],
        drone_utilization=result["drone_utilization"],
        truck_utilization=result["truck_utilization"],
        serving_pct=result["serving_pct"],
        stockouts=result["stockouts"],
        urgent_stockouts=result["urgent_stockouts"],
        nonurgent_stockouts=result["nonurgent_stockouts"],
        small_utilization=wtu.get("small"),
        medium_utilization=wtu.get("medium"),
        large_utilization=wtu.get("large"),
        drone_cost=result["drone_cost"],
        truck_cost=result["truck_cost"],
        overhead_cost=result["overhead_cost"],
        outsource_expenses=outsource_expenses,
        outsource_revenue=outsource_revenue,
        disruption_events=json.dumps(result["disruption_events"]),
    )
    db.add(qr)

    # Mirror into Play.quarterly_results JSON
    qr_list = json.loads(play.quarterly_results or "[]")
    qr_list.append({
        "quarter":                  ran_quarter,
        "revenue":                  result["revenue"],
        "urgent_revenue":           result["urgent_revenue"],
        "nonurgent_revenue":        result["nonurgent_revenue"],
        "operating_cost":           result["operating_cost"],
        "profit":                   result["profit"],
        "cash_after":               play.cash,
        "orders_fulfilled":         result["orders_fulfilled"],
        "orders_total":             result["orders_total"],
        "utilization_rate":         result["utilization_rate"],
        "fleet_capacity":           result["fleet_capacity"],
        "capex_warehouses":         result["capex_warehouses"],
        "capex_vehicles":           result["capex_vehicles"],
        "drone_utilization":        result["drone_utilization"],
        "truck_utilization":        result["truck_utilization"],
        "serving_pct":              result["serving_pct"],
        "stockouts":                result["stockouts"],
        "urgent_stockouts":         result["urgent_stockouts"],
        "nonurgent_stockouts":      result["nonurgent_stockouts"],
        "urgent_fulfilled":         result["urgent_fulfilled"],
        "nonurgent_fulfilled":      result["nonurgent_fulfilled"],
        "warehouse_type_utilization": result["warehouse_type_utilization"],
        "drone_cost":               result["drone_cost"],
        "truck_cost":               result["truck_cost"],
        "overhead_cost":            result["overhead_cost"],
        "outsource_expenses":       outsource_expenses,
        "outsource_revenue":        outsource_revenue,
        "disruption_events":        result["disruption_events"],
    })
    play.quarterly_results = json.dumps(qr_list)

    # Advance or complete
    next_q = ran_quarter
    if ran_quarter >= sc.total_quarters:
        play.completed = True
    else:
        play.current_quarter = ran_quarter + 1
        next_q = play.current_quarter
        # Advance warehouse builds to the new quarter so they are active for the planning phase
        advance_warehouse_builds(play.warehouses, next_q, wh_cfg)

    try:
        db.commit()
    except IntegrityError:
        # Two group members hit Run Quarter at the same instant. The row
        # lock above closes this race on Postgres; on SQLite (no row-level
        # locking) the uq_quarter_results_play_quarter constraint is the
        # backstop instead — surface a clean retry rather than a raw 500.
        db.rollback()
        raise HTTPException(409, "This quarter was just advanced by a teammate. Please refresh and try again.")

    # Return updated warehouse states relative to the next planning quarter (next_q)
    # so the frontend planning state is correctly initialized as active/ready.
    warehouse_states = [
        {
            "id":                    wh.id,
            "slot_id":               wh.slot_id,
            "warehouse_type":        wh.warehouse_type,
            "is_active":             wh.is_active,
            "is_sold":               wh.is_sold,
            "built_at_quarter":      wh.built_at_quarter,
            "quarters_until_active": quarters_until_active(wh, wh_cfg, next_q),
            "activates_next_run":    (not wh.is_active) and quarters_until_active(wh, wh_cfg, next_q + 1) == 0,
            "vehicle_counts":        count_vehicles(wh.vehicles),
            "vehicles":              json.loads(wh.vehicles or "[]"),
        }
        for wh in play.warehouses if not wh.is_sold
    ]

    return {
        **result,
        "quarter":          qr.quarter,
        "cash_after":       play.cash,
        "pending_vehicle_net_cost": 0.0,
        "completed":        play.completed,
        "next_quarter":     play.current_quarter,
        "warehouse_states": warehouse_states,
    }


@router.get("/sessions/{play_id}/history", tags=["student"])
def get_history(play_id: str, db: Session = Depends(get_db), email: str = Depends(require_student_email)):
    """Full P&L history for the sidebar."""
    play = _get_play_or_404(play_id, db)
    _require_member(db, play, email)
    return json.loads(play.quarterly_results or "[]")
