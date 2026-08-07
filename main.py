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

# Create all DB tables on startup
Base.metadata.create_all(bind=engine)

# Self-healing migration for QuarterResult new columns in production DB (Postgres/SQLite)
from sqlalchemy import text
with engine.connect() as conn:
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
    ]
    for col_name, col_type, default_val in columns_to_add:
        try:
            conn.execute(text(f"ALTER TABLE quarter_results ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"))
            conn.commit()
        except Exception:
            pass

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
    return FileResponse(STATIC_DIR / "game.html")

@app.get("/instructor", include_in_schema=False)
def serve_instructor():
    """Serve the instructor dashboard."""
    return FileResponse(STATIC_DIR / "instructor_dashboard.html")

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "version": "0.2.0"}
