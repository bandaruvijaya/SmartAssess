#!/bin/bash

echo "Starting SmartAssess (FastAPI) on single port..."

# Set default port if not provided
export PORT=${PORT:-5000}

echo "FastAPI will run on port: $PORT"

# Start FastAPI (serves API and frontend)
uvicorn api.main:app --host 0.0.0.0 --port $PORT --log-level info
