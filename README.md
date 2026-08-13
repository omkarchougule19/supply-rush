# Supply Rush

A supply chain education game for classroom use.

> [!IMPORTANT]
> **AI Coding Assistant Guidelines:**
> This repository is configured with project-scoped rules for AI agents in [.agents/AGENTS.md](.agents/AGENTS.md). 
> All AI coding assistants must:
> 1. Read [rules.txt](rules.txt) first before proposing or implementing changes.
> 2. Document the design decisions and implementation steps under **Section 7 (Development Log)** of [rules.txt](rules.txt).

## Quick Start (Local)

```bash
cd backend
bash run.sh
```

Then open:
- **Student game** → http://localhost:8000
- **Instructor dashboard** → http://localhost:8000/instructor
- **API docs** → http://localhost:8000/docs

## Manual Setup

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

## Project Structure

```
backend/
  main.py          # FastAPI app, static serving, page routes
  models.py        # DB models: Scenario, Play, PlacedWarehouse, QuarterResult
  routes.py        # All API endpoints
  game_logic.py    # Demand generation, quarter simulation
  schemas.py       # Pydantic request/response schemas
  database.py      # DB setup (SQLite → PostgreSQL swap is one .env line)
  requirements.txt
  Dockerfile
  .env.example
  static/
    game.html               # Student game
    instructor_dashboard.html
    chicago_zones.png       # Map image
```

## Game Flow

**Normal mode:** Open http://localhost:8000 → Play Now → game starts with defaults

**Instructor mode:**
1. Instructor opens http://localhost:8000/instructor
2. Creates a scenario, configures all variables → gets a 6-char code (e.g. `RUSH42`)
3. Shares code with students
4. Students open http://localhost:8000 → enter code → play with instructor's config

## Production Deployment

Set these env vars on your host:
```
DATABASE_URL=postgresql://user:pass@host:5432/supply_rush
ALLOWED_ORIGINS=https://yourdomain.com
HIDE_DOCS=true
```

Then run with Docker:
```bash
docker build -t supply-rush .
docker run -p 8000:8000 --env-file .env supply-rush
```
