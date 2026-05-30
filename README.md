# one-task-litellm-codex-logger

A minimal, single-container MVP that runs **one** Codex CLI coding task and
logs every LLM API call to a JSONL file. The traffic flows through a
LiteLLM Proxy that sits in front of OpenAI, and a LiteLLM custom callback
appends one structured record per call.

## What this MVP does

- Spins up **LiteLLM Proxy** on `http://127.0.0.1:4000/v1` inside the
  container.
- Generates a **temporary Codex CLI config** inside the container that points
  Codex at the local LiteLLM proxy.
- Runs **one** coding task with `codex exec` against `/workspace`.
- Captures, per LLM call, a JSON record in
  `/logs/${TASK_ID}/requests.jsonl` via a LiteLLM `CustomLogger` callback.
- Saves Codex stdout/stderr, LiteLLM stdout/stderr, a git diff of the
  workspace, and a `final_result.json` summary.

## What this MVP does NOT do

- Does not mount or read the host's `~/.codex` directory.
- Does not rely on the host's local Codex configuration.
- Does not do packet capture, `tcpdump`, Wireshark, or TLS MITM.
- Does not capture hidden provider-side system prompts.
- Does not capture model chain-of-thought.
- Does not run SWE-bench grading or parallel rollouts.
- Does not run more than one task at a time.

## Why LiteLLM, not packet capture

We want **client-side** structured visibility into what the agent is sending
to the model and what comes back. LiteLLM is an OpenAI-compatible reverse
proxy with a first-class callback API, so we can attach a Python logger and
get already-parsed `kwargs` / `response_obj` payloads without dealing with
HTTPS interception, certificate trust, or byte-level reassembly.

## Request flow

```
Codex CLI
  -> LiteLLM Proxy at http://127.0.0.1:4000/v1
  -> Real OpenAI API
  -> LiteLLM Proxy
  -> Codex CLI
```

**LiteLLM does not call Codex.** Codex calls LiteLLM. The callback fires on
LiteLLM's side after each upstream call returns (or fails).

## The two API keys

| Variable | Used by | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | LiteLLM only | Real upstream OpenAI key used by LiteLLM to call OpenAI. **Codex never sees it.** |
| `LITELLM_MASTER_KEY` | Codex CLI | Local proxy key Codex sends to LiteLLM. Defaults to `sk-local-dev`. Redacted from logs. |

## Build

```bash
docker build -t one-task-litellm-codex-logger:latest .
```

## Run one task

Mount a local repo at `/workspace` and a log directory at `/logs`:

```bash
docker run --rm \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e LITELLM_MASTER_KEY="sk-local-dev" \
  -e TASK_ID="task_001" \
  -e SESSION_ID="session_001" \
  -e CODEX_MODEL="gpt-5-codex" \
  -e TASK_PROMPT="Fix the bug in this repository and run tests." \
  -v "$PWD/my_task_repo:/workspace" \
  -v "$PWD/logs:/logs" \
  one-task-litellm-codex-logger:latest
```

### Environment variables

| Name | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | (required) | Real OpenAI key. Container exits if unset. |
| `LITELLM_MASTER_KEY` | `sk-local-dev` | Key Codex uses to talk to LiteLLM. |
| `TASK_ID` | `task_001` | Becomes the log subdirectory name. |
| `SESSION_ID` | `session_001` | Stamped into every JSONL record. |
| `CODEX_MODEL` | `gpt-5-codex` | Sent verbatim to LiteLLM; LiteLLM forwards to OpenAI as `openai/<model>`. |
| `TASK_PROMPT` | (sane default) | Passed as the positional argument to `codex exec`. |

## Output layout

```
logs/
  task_001/
    requests.jsonl        # one JSON object per LiteLLM-tracked LLM call
    litellm_stdout.log    # LiteLLM server stdout
    litellm_stderr.log    # LiteLLM server stderr
    agent_stdout.log      # Codex JSON/stdout output
    agent_stderr.log      # Codex stderr output
    diff.patch            # final `git diff` after Codex runs (empty if no .git)
    final_result.json     # task/session metadata + final exit status
```

### `requests.jsonl` schema

One JSON object per line. Success record:

```json
{
  "task_id": "task_001",
  "session_id": "session_001",
  "request_id": "...",
  "timestamp_start": "2026-05-30T12:00:00+00:00",
  "timestamp_end":   "2026-05-30T12:00:01+00:00",
  "latency_ms": 1234.5,
  "model": "gpt-5-codex",
  "call_type": "acompletion",
  "kwargs": { "... redacted, JSON-safe ..." },
  "response_obj": { "... redacted, JSON-safe ..." },
  "status": "success",
  "error": null,
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
  "response_cost": 0.0
}
```

Failure record:

```json
{
  "task_id": "task_001",
  "session_id": "session_001",
  "request_id": "...",
  "timestamp_start": "...",
  "timestamp_end": "...",
  "latency_ms": 1234.5,
  "model": "gpt-5-codex",
  "call_type": "acompletion",
  "kwargs": { "..." },
  "response_obj": null,
  "status": "failure",
  "error": "RateLimitError: ...",
  "usage": null,
  "response_cost": null
}
```

### `final_result.json` schema

```json
{
  "task_id": "task_001",
  "session_id": "session_001",
  "agent_exit_code": 0,
  "duration_seconds": 123.4,
  "status": "success"
}
```

`status` is `"failed"` if Codex exited non-zero. The file is written even
when Codex fails.

## Secret redaction

`custom_callbacks.py` walks every dict / list before serialization and
replaces values under any of these keys (case-insensitive) with
`***REDACTED***`:

```
authorization, api_key, api-key, apikey, openai_api_key,
cookie, set-cookie, token, bearer, master_key, litellm_master_key,
password, secret, x-api-key, proxy-authorization
```

Free-form string values that match common API-key shapes (`sk-...`,
`Bearer ...`, `ghp_...`, `xoxb-...`) are also masked, so an `OPENAI_API_KEY`
or `LITELLM_MASTER_KEY` value that ends up nested in a body is still
scrubbed.

## Validation

You can verify LiteLLM logging without running Codex.

### Option A: from inside the container

Start the container with an interactive shell:

```bash
docker run --rm -it --entrypoint bash \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e LITELLM_MASTER_KEY="sk-local-dev" \
  -e TASK_ID="task_001" \
  -v "$PWD/logs:/logs" \
  one-task-litellm-codex-logger:latest
```

Inside the container:

```bash
litellm --config /app/litellm_config.yaml --host 127.0.0.1 --port 4000 &
sleep 5
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
cat /logs/task_001/requests.jsonl
```

### Option B: publish port 4000 to the host

```bash
docker run --rm -p 4000:4000 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e LITELLM_MASTER_KEY="sk-local-dev" \
  -v "$PWD/logs:/logs" \
  --entrypoint bash one-task-litellm-codex-logger:latest \
  -c "litellm --config /app/litellm_config.yaml --host 0.0.0.0 --port 4000"
```

Then from the host:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
tail -1 logs/task_001/requests.jsonl
```

## Configuration assumptions

These are the syntactic choices made by this MVP. Adjust if your installed
versions diverge.

### LiteLLM config (`litellm_config.yaml`)

```yaml
model_list:
  - model_name: "*"
    litellm_params:
      model: openai/*
      api_key: os.environ/OPENAI_API_KEY

litellm_settings:
  callbacks: custom_callbacks.proxy_handler_instance
  drop_params: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

- `model_name: "*"` + `model: openai/*` is the LiteLLM wildcard route: any
  model the client asks for is forwarded to OpenAI under that exact name.
- `callbacks` points at the module attribute `proxy_handler_instance` in
  `custom_callbacks.py`. The proxy imports it from `/app` (added to
  `PYTHONPATH` by both the Dockerfile and `run_task.sh`).
- `general_settings.master_key` is the proxy key the client (Codex) must
  send. `OPENAI_API_KEY` is **only** used by LiteLLM when calling OpenAI.

### Codex CLI config (`/root/.codex/config.toml`)

Generated fresh inside the container on every run:

```toml
model = "${CODEX_MODEL}"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM Local Proxy"
base_url = "http://127.0.0.1:4000/v1"
env_key = "LITELLM_MASTER_KEY"
wire_api = "responses"
```

- `env_key = "LITELLM_MASTER_KEY"` tells Codex to read the bearer token
  from that env var, not from `OPENAI_API_KEY`.
- `wire_api = "responses"` matches Codex's current default of using
  OpenAI's `/v1/responses` endpoint shape; if your Codex build only
  supports chat completions, change to `"chat"` and re-run.

## Current limitations

- This captures LiteLLM **callback-level** structured payloads, not
  necessarily raw byte-for-byte HTTP bodies. If LiteLLM's callback does not
  expose the original HTTP request body, what you get is the parsed call
  payload that LiteLLM constructed.
- This does not capture OpenAI hidden provider-side prompts.
- This does not capture the model's internal chain-of-thought.
- Only one task / one session per container.
- No SWE-bench grading.
- No parallel rollouts.
- Streaming behavior depends on the installed LiteLLM version's callback
  support and may need refinement (e.g. assembling chunks before logging).
- Codex CLI's config schema is moving — if `env_key` / `wire_api` are
  renamed in a future release, update `run_task.sh` accordingly. The
  syntax above is what this MVP writes.
