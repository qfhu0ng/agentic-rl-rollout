#!/usr/bin/env bash
# Run one coding task with Codex CLI -> LiteLLM Proxy -> OpenAI.
# Logs the trajectory to /logs/${TASK_ID}/.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
: "${TASK_ID:=task_001}"
: "${SESSION_ID:=session_001}"
: "${LITELLM_MASTER_KEY:=sk-local-dev}"
: "${CODEX_MODEL:=gpt-5-codex}"
: "${CODEX_SANDBOX:=workspace-write}"
: "${TASK_PROMPT:=Solve this coding task. Inspect the repository, make necessary changes, and run tests if appropriate.}"
: "${WORKDIR:=/workspace}"
: "${LOG_ROOT:=/logs}"
: "${LITELLM_PORT:=4000}"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: OPENAI_API_KEY is not set. LiteLLM needs it to call OpenAI." >&2
  exit 2
fi

export TASK_ID SESSION_ID LITELLM_MASTER_KEY OPENAI_API_KEY LOG_ROOT

TASK_LOG_DIR="${LOG_ROOT}/${TASK_ID}"
mkdir -p "${TASK_LOG_DIR}"

LITELLM_STDOUT="${TASK_LOG_DIR}/litellm_stdout.log"
LITELLM_STDERR="${TASK_LOG_DIR}/litellm_stderr.log"
AGENT_STDOUT="${TASK_LOG_DIR}/agent_stdout.log"
AGENT_STDERR="${TASK_LOG_DIR}/agent_stderr.log"
DIFF_PATH="${TASK_LOG_DIR}/diff.patch"
RESULT_PATH="${TASK_LOG_DIR}/final_result.json"

# ---------------------------------------------------------------------------
# Start LiteLLM Proxy
# ---------------------------------------------------------------------------
echo "[run_task] Starting LiteLLM Proxy on 127.0.0.1:${LITELLM_PORT}..."
# LiteLLM imports custom_callbacks; make /app importable.
export PYTHONPATH="/app:${PYTHONPATH:-}"

litellm \
  --config /app/litellm_config.yaml \
  --host 127.0.0.1 \
  --port "${LITELLM_PORT}" \
  >"${LITELLM_STDOUT}" 2>"${LITELLM_STDERR}" &
LITELLM_PID=$!

cleanup() {
  if kill -0 "${LITELLM_PID}" 2>/dev/null; then
    echo "[run_task] Stopping LiteLLM (pid=${LITELLM_PID})..."
    kill "${LITELLM_PID}" 2>/dev/null || true
    # Give it a moment to flush logs.
    for _ in 1 2 3 4 5; do
      kill -0 "${LITELLM_PID}" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "${LITELLM_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Wait for the proxy to be ready (poll /health/liveliness, fall back to /v1/models).
echo "[run_task] Waiting for LiteLLM to become ready..."
READY=0
for i in $(seq 1 60); do
  if ! kill -0 "${LITELLM_PID}" 2>/dev/null; then
    echo "[run_task] LiteLLM exited early. See ${LITELLM_STDERR}." >&2
    exit 3
  fi
  if curl -sf "http://127.0.0.1:${LITELLM_PORT}/health/liveliness" >/dev/null 2>&1 \
     || curl -sf "http://127.0.0.1:${LITELLM_PORT}/health/readiness" >/dev/null 2>&1 \
     || curl -sf -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
        "http://127.0.0.1:${LITELLM_PORT}/v1/models" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "${READY}" -ne 1 ]]; then
  echo "[run_task] LiteLLM did not become ready within 60s." >&2
  exit 4
fi
echo "[run_task] LiteLLM is ready."

# ---------------------------------------------------------------------------
# Generate Codex CLI config (temporary, inside container)
# ---------------------------------------------------------------------------
mkdir -p /root/.codex
cat >/root/.codex/config.toml <<EOF
model = "${CODEX_MODEL}"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM Local Proxy"
base_url = "http://127.0.0.1:${LITELLM_PORT}/v1"
env_key = "LITELLM_MASTER_KEY"
wire_api = "responses"
EOF

echo "[run_task] Generated /root/.codex/config.toml:"
sed 's/^/    /' /root/.codex/config.toml

# Codex reads LITELLM_MASTER_KEY (via env_key); make sure OPENAI_API_KEY is
# NOT what Codex uses to authenticate against the proxy.
export LITELLM_MASTER_KEY

# ---------------------------------------------------------------------------
# Run Codex
# ---------------------------------------------------------------------------
echo "[run_task] Running Codex on prompt: ${TASK_PROMPT}"
START_TS=$(date +%s)
AGENT_EXIT=0
set +e
codex exec \
  --cd "${WORKDIR}" \
  --json \
  --sandbox "${CODEX_SANDBOX}" \
  "${TASK_PROMPT}" \
  >"${AGENT_STDOUT}" 2>"${AGENT_STDERR}"
AGENT_EXIT=$?
set -e
END_TS=$(date +%s)
DURATION=$((END_TS - START_TS))

# ---------------------------------------------------------------------------
# Capture diff
# ---------------------------------------------------------------------------
if [[ -d "${WORKDIR}/.git" ]]; then
  git -C "${WORKDIR}" diff >"${DIFF_PATH}" 2>/dev/null || echo "" >"${DIFF_PATH}"
else
  echo "" >"${DIFF_PATH}"
fi

# ---------------------------------------------------------------------------
# final_result.json (always written)
# ---------------------------------------------------------------------------
if [[ "${AGENT_EXIT}" -eq 0 ]]; then
  STATUS="success"
else
  STATUS="failed"
fi

python3 - <<PY
import json, os
result = {
    "task_id": os.environ["TASK_ID"],
    "session_id": os.environ["SESSION_ID"],
    "agent_exit_code": int("${AGENT_EXIT}"),
    "duration_seconds": float(${DURATION}),
    "status": "${STATUS}",
}
with open("${RESULT_PATH}", "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2)
PY

echo "[run_task] Done. Logs in ${TASK_LOG_DIR}/"
ls -la "${TASK_LOG_DIR}" || true

exit "${AGENT_EXIT}"
