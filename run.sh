#!/bin/bash
# Run the Sentinel Hub Downloader web app
cd "$(dirname "$0")"

echo "Starting Sentinel Hub Downloader at http://localhost:8080"
venv/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
