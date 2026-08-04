#!/bin/bash
# Supply Rush — local dev server
# Usage: bash run.sh

cd "$(dirname "$0")"

# Create .env if missing
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

# Install deps if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  pip install -r requirements.txt
fi

echo ""
echo "  Supply Rush starting..."
echo "  Game:       http://localhost:8000"
echo "  Instructor: http://localhost:8000/instructor"
echo "  API docs:   http://localhost:8000/docs"
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000
