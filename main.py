"""
main.py — Supply Rush FastAPI application
Run locally:  uvicorn main:app --reload
Production:   gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app
"""

import json
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from fastapi import Depends

from database import engine, Base
from routes import router
from auth import authenticate_instructor
from game_logic import DEFAULT_DISRUPTION_CONFIG

import logging

# Configure logger
logger = logging.getLogger("uvicorn.error")

# Try to create all DB tables and apply self-healing migrations on startup
try:
    Base.metadata.create_all(bind=engine)

    # Self-healing migrations for production DB (Postgres/SQLite)
    from sqlalchemy import text

    def _add_column_if_missing(conn, table, col_name, col_type, default_val):
        """ADD COLUMN fails every single boot once the column already exists
        (the overwhelmingly common case) — that's expected and stays silent.
        Anything else (permissions, disk full, type conflict) is a real
        migration failure and gets logged, or it'd fail silently forever and
        surface later as a confusing "no such column" error far from the
        actual cause."""
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"))
            conn.commit()
        except Exception as e:
            conn.rollback()  # reset aborted transaction state before the next column (critical for PostgreSQL)
            msg = str(e).lower()
            if "already exists" not in msg and "duplicate column" not in msg:
                logger.error(f"Migration failed adding {table}.{col_name} — this column may be missing at runtime: {str(e)}")

    with engine.connect() as conn:
        # 1. quarter_results table migrations
        columns_to_add = [
            ("urgent_revenue",      "FLOAT",   "0.0"),
            ("nonurgent_revenue",   "FLOAT",   "0.0"),
            ("drone_utilization",   "FLOAT",   "0.0"),
            ("truck_utilization",   "FLOAT",   "0.0"),
            ("urgent_stockouts",    "INTEGER", "0"),
            ("nonurgent_stockouts", "INTEGER", "0"),
            # v3 additions — per-warehouse-type utilization & per-vehicle-type cost
            ("small_utilization",   "FLOAT",   "NULL"),
            ("medium_utilization",  "FLOAT",   "NULL"),
            ("large_utilization",   "FLOAT",   "NULL"),
            ("drone_cost",          "FLOAT",   "0.0"),
            ("truck_cost",          "FLOAT",   "0.0"),
            # Outsourcing additions
            ("outsource_expenses",  "FLOAT",   "0.0"),
            ("outsource_revenue",   "FLOAT",   "0.0"),
            # Warehouse overhead cost
            ("overhead_cost",       "FLOAT",   "0.0"),
            # Random disruption events fired (and affecting this play) that quarter
            ("disruption_events",   "TEXT",    "'[]'"),
        ]
        for col_name, col_type, default_val in columns_to_add:
            _add_column_if_missing(conn, "quarter_results", col_name, col_type, default_val)

        # 2. scenarios table migrations (sell restrictions & outsourcing)
        scenario_columns = [
            ("allow_sell_warehouses", "BOOLEAN", "TRUE"),
            ("allow_sell_trucks",     "BOOLEAN", "TRUE"),
            ("allow_sell_drones",     "BOOLEAN", "TRUE"),
            ("allow_outsourcing",     "BOOLEAN", "FALSE"),
            ("outsource_cost_urgent", "FLOAT",   "75.0"),
            ("outsource_cost_nonurgent", "FLOAT", "40.0"),
            ("allow_moving_vehicles", "BOOLEAN", "FALSE"),
            ("unlocked_quarter",       "INTEGER", "0"),
            # Random disruption events config — off by default, see DEFAULT_DISRUPTION_CONFIG
            ("disruption_config",      "TEXT",    f"'{json.dumps(DEFAULT_DISRUPTION_CONFIG)}'"),
        ]
        for col_name, col_type, default_val in scenario_columns:
            _add_column_if_missing(conn, "scenarios", col_name, col_type, default_val)

        # 3. plays table migrations
        plays_columns = [
            ("outsourced_zones", "TEXT", "'[]'"),
            ("setup_capex_warehouses", "FLOAT", "0.0"),
            ("setup_capex_vehicles",   "FLOAT", "0.0"),
            ("group_name", "VARCHAR(120)", "NULL"),
        ]
        for col_name, col_type, default_val in plays_columns:
            _add_column_if_missing(conn, "plays", col_name, col_type, default_val)

        # 4. placed_warehouses table migrations
        placed_warehouses_columns = [
            ("purchase_price_paid", "FLOAT", "NULL"),
        ]
        for col_name, col_type, default_val in placed_warehouses_columns:
            _add_column_if_missing(conn, "placed_warehouses", col_name, col_type, default_val)

        # 5. Partial unique index closing the race where two group members
        #    submitting POST /sessions for the same (scenario, group_name)
        #    at the same moment could each create their own Play row instead
        #    of sharing one (the app-level find-or-create check-then-insert
        #    is not atomic without this). Scoped to incomplete plays only —
        #    a group name may legitimately be reused once its play is done.
        completed_false = "0" if engine.dialect.name == "sqlite" else "false"
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_plays_active_group "
                "ON plays (scenario_id, lower(group_name)) "
                f"WHERE completed = {completed_false} AND group_name IS NOT NULL"
            ))
            conn.commit()
        except Exception as e:
            conn.rollback()  # e.g. pre-existing duplicate rows on an old DB — best-effort, matches migrations above
            logger.error(
                f"Could not create ix_plays_active_group unique index — group-play duplicate-creation race is "
                f"UNPROTECTED until this is resolved: {str(e)}"
            )

        # 6. Unique index on quarter_results(play_id_fk, quarter) — DB-level
        #    backstop against duplicate quarter rows (belt-and-suspenders
        #    alongside advance_quarter's row lock; matters on an old DB where
        #    this table predates the lock and the UniqueConstraint above).
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_quarter_results_play_quarter "
                "ON quarter_results (play_id_fk, quarter)"
            ))
            conn.commit()
        except Exception as e:
            conn.rollback()  # e.g. pre-existing duplicate quarter rows on an old DB — best-effort
            logger.error(
                f"Could not create uq_quarter_results_play_quarter unique index — duplicate quarter rows are "
                f"UNPROTECTED at the DB level until this is resolved: {str(e)}"
            )
except Exception as e:
    logger.error(f"Database initialization/migration failed on startup: {str(e)}")


app = FastAPI(
    title="Supply Rush API",
    description="Backend for Supply Rush — supply chain education game",
    version="0.2.0",
    # Hide docs in production if needed — set HIDE_DOCS=true env var
    docs_url=None if os.getenv("HIDE_DOCS") == "true" else "/docs",
    redoc_url=None,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
# In production set ALLOWED_ORIGINS env var to your actual domain.
# allow_credentials must stay False while origins is the "*" default: Starlette
# reflects the request's actual Origin (not a literal "*") whenever wildcard
# origins are combined with allow_credentials=True, which would let any site
# ride a browser's cached instructor Basic-Auth credentials or session cookie
# cross-origin. Only turn credentials on once ALLOWED_ORIGINS names real domains.
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    # "*" anywhere in the list (not just as the sole entry) makes Starlette
    # treat every origin as allowed, so a leftover wildcard alongside a real
    # domain (e.g. a forgotten "*" left in after adding the production
    # domain) must still disable credentials.
    allow_credentials="*" not in allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(router, prefix="/api")

# ── Static files (images, etc.) ───────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# instructor_dashboard.html deliberately lives OUTSIDE the /static mount (which
# is served with no auth check on the raw file) — it must only ever be
# reachable through /instructor, which requires HTTP Basic Auth below. Do not
# move it back under static/ or add another route/mount that can reach it.
PRIVATE_DIR = Path(__file__).parent / "private"

# ── Page routes ───────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def serve_game():
    """Serve the student game."""
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    return FileResponse(STATIC_DIR / "game.html", headers=headers)

@app.get("/instructor", include_in_schema=False)
def serve_instructor(username: str = Depends(authenticate_instructor)):
    """Serve the instructor dashboard — gated by HTTP Basic Auth (env-var credentials)."""
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    return FileResponse(PRIVATE_DIR / "instructor_dashboard.html", headers=headers)

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "version": "0.2.0"}


# ── Database Exception Handlers ───────────────────────────────────────────────
from sqlalchemy.exc import DBAPIError, OperationalError
from fastapi.responses import JSONResponse
from fastapi import Request

@app.exception_handler(OperationalError)
async def db_operational_exception_handler(request: Request, exc: OperationalError):
    # Handle DB connection drop, invalid credentials, database server down
    logger.error(f"Database operational error at {request.url.path}: {str(exc)}")
    return JSONResponse(
        status_code=503,
        content={"detail": "Database is temporarily unavailable. Please try again in a few moments."},
    )

@app.exception_handler(DBAPIError)
async def db_api_exception_handler(request: Request, exc: DBAPIError):
    # Handle general database query / driver exceptions
    logger.error(f"Database query error at {request.url.path}: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": "A database error occurred while processing your request. Please try again."},
    )
