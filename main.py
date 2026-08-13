"""
main.py — Supply Rush FastAPI application
Run locally:  uvicorn main:app --reload
Production:   gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app
"""

import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import engine, Base
from routes import router

import logging

# Configure logger
logger = logging.getLogger("uvicorn.error")

# Try to create all DB tables and apply self-healing migrations on startup
try:
    Base.metadata.create_all(bind=engine)

    # Self-healing migrations for production DB (Postgres/SQLite)
    from sqlalchemy import text
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
        ]
        for col_name, col_type, default_val in columns_to_add:
            try:
                conn.execute(text(f"ALTER TABLE quarter_results ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"))
                conn.commit()
            except Exception:
                conn.rollback()  # Reset aborted transaction state before next column (critical for PostgreSQL)

        # 2. scenarios table migrations (sell restrictions & outsourcing)
        scenario_columns = [
            ("allow_sell_warehouses", "BOOLEAN", "TRUE"),
            ("allow_sell_trucks",     "BOOLEAN", "TRUE"),
            ("allow_sell_drones",     "BOOLEAN", "TRUE"),
            ("allow_outsourcing",     "BOOLEAN", "FALSE"),
            ("outsource_cost_urgent", "FLOAT",   "75.0"),
            ("outsource_cost_nonurgent", "FLOAT", "40.0"),
            ("allow_moving_vehicles", "BOOLEAN", "FALSE"),
        ]
        for col_name, col_type, default_val in scenario_columns:
            try:
                conn.execute(text(f"ALTER TABLE scenarios ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"))
                conn.commit()
            except Exception:
                conn.rollback()

        # 3. plays table migrations
        plays_columns = [
            ("outsourced_zones", "TEXT", "'[]'"),
        ]
        for col_name, col_type, default_val in plays_columns:
            try:
                conn.execute(text(f"ALTER TABLE plays ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"))
                conn.commit()
            except Exception:
                conn.rollback()
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
# In production set ALLOWED_ORIGINS env var to your actual domain
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(router, prefix="/api")

# ── Static files (images, etc.) ───────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Page routes ───────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def serve_game():
    """Serve the student game."""
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    return FileResponse(STATIC_DIR / "game.html", headers=headers)

@app.get("/instructor", include_in_schema=False)
def serve_instructor():
    """Serve the instructor dashboard."""
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    return FileResponse(STATIC_DIR / "instructor_dashboard.html", headers=headers)

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
