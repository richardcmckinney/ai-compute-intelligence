#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${ACI_DEMO_PORT:-8010}"
BASE_URL="http://127.0.0.1:${PORT}"
SERVER_LOG="${REPO_ROOT}/.demo-smoke.log"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

cd "${REPO_ROOT}"
ACI_DEMO_PORT="${PORT}" ACI_DEMO_RELOAD=0 ./scripts/run_demo.sh >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

for _ in {1..60}; do
  if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -fsS "${BASE_URL}/health" >/dev/null
curl -fsS "${BASE_URL}/ready" >/dev/null
curl -fsS "${BASE_URL}/v1/dashboard/overview" >/dev/null
curl -fsS "${BASE_URL}/v1/attribution/customer-support-bot" >/dev/null

INTERCEPT_RESPONSE="$(curl -fsS -X POST "${BASE_URL}/v1/intercept" \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "demo-smoke-enriched-1",
    "model": "gpt-4o-mini",
    "provider": "openai",
    "service_name": "customer-support-bot",
    "input_tokens": 600,
    "max_tokens": 300,
    "estimated_cost_usd": 0.003,
    "environment": "staging"
  }')"

printf '%s\n' "${INTERCEPT_RESPONSE}" | grep -q '"outcome":"enriched"\|"outcome":"soft_stopped"\|"outcome":"redirected"'

echo "Demo smoke passed against ${BASE_URL}"
