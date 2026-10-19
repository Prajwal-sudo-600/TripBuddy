#!/usr/bin/env bash
# Reset TripBuddy database to initial seed state and generate a fresh canary.
set -euo pipefail

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

echo "Resetting TripBuddy at http://${HOST}:${PORT}/admin/reset ..."
curl -s -X POST "http://${HOST}:${PORT}/admin/reset" \
  -H "Content-Type: application/json" \
  -d '{}'
echo ""
