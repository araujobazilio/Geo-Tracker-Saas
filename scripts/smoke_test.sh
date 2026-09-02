#!/usr/bin/env bash
# =============================================================================
# GEO Tracker — Deployment smoke test (zero-cost paths only)
# =============================================================================
# Tests only zero-cost paths:
#   GET /health
#   GET /ready
#   GET /login
#
# Does NOT create a real measurement.
# Does NOT consume AI Checks.
# Does NOT call paid providers.
#
# Usage:
#   ./scripts/smoke_test.sh [base_url]
#
# Default base URL: http://localhost:8000
# =============================================================================

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
FAIL=0

echo "GEO Tracker — Deployment Smoke Test"
echo "Target: ${BASE_URL}"
echo "=" * 40

# 1. Health check (liveness, no dependencies).
echo -n "GET /health ... "
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/health" || true)
if [[ "$HEALTH_STATUS" == "200" ]]; then
    echo "OK (200)"
else
    echo "FAIL ($HEALTH_STATUS)"
    FAIL=1
fi

# 2. Ready check (PostgreSQL + Redis).
echo -n "GET /ready ... "
READY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/ready" || true)
if [[ "$READY_STATUS" == "200" ]]; then
    echo "OK (200)"
else
    echo "FAIL ($READY_STATUS)"
    FAIL=1
fi

# 3. Login page (web UI renders).
echo -n "GET /login ... "
LOGIN_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/login" || true)
if [[ "$LOGIN_STATUS" == "200" ]]; then
    echo "OK (200)"
else
    echo "FAIL ($LOGIN_STATUS)"
    FAIL=1
fi

echo "=" * 40
if [[ "$FAIL" -eq 0 ]]; then
    echo "All smoke tests PASSED."
    exit 0
else
    echo "One or more smoke tests FAILED."
    exit 1
fi
