# agentic-rl-rollout

Logs every LLM API call from Codex CLI to a structured JSONL file, using LiteLLM Proxy as an in-container interceptor.

## Architecture

```
Codex CLI
  → LiteLLM Proxy (127.0.0.1:4000)   ← custom callback writes JSONL here
  → OpenAI API
```

- `OPENAI_API_KEY` — used only by LiteLLM to call OpenAI. Codex never sees it.
- `LITELLM_MASTER_KEY` — local proxy key Codex uses. Defaults to `sk-local-dev`.

## Setup

```bash
# 1. Clone repo and set up flask workspace (SWE-bench base commit)
bash setup_workspace.sh

# 2. Build image
docker build -t one-task-litellm-codex-logger:latest .
```

## Run

```bash
docker run --rm \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e LITELLM_MASTER_KEY="sk-local-dev" \
  -e TASK_ID="task_001" \
  -e CODEX_MODEL="gpt-5-codex" \
  -e CODEX_SANDBOX="danger-full-access" \
  -e TASK_PROMPT="Read /task/task.md and solve the task in /workspace." \
  -v "$PWD/workspace/flask:/workspace" \
  -v "$PWD/logs:/logs" \
  -v "$PWD/task:/task:ro" \
  one-task-litellm-codex-logger:latest
```

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | required | Real OpenAI key |
| `LITELLM_MASTER_KEY` | `sk-local-dev` | Proxy auth key for Codex |
| `TASK_ID` | `task_001` | Log subdirectory name |
| `CODEX_MODEL` | `gpt-5-codex` | Forwarded to OpenAI as-is |
| `CODEX_SANDBOX` | `workspace-write` | Use `danger-full-access` if Codex needs pip/network |
| `TASK_PROMPT` | (default prompt) | Passed to `codex exec` |

## Output

```
logs/task_001/
  requests.jsonl      # one JSON record per LLM call (request + response + usage + cost)
  agent_stdout.log    # Codex event stream
  diff.patch          # git diff of workspace after run
  final_result.json   # exit code, duration, status
  litellm_stdout.log
  litellm_stderr.log
  agent_stderr.log
```

Each `requests.jsonl` record:
```json
{
  "task_id": "task_001",
  "session_id": "session_001",
  "request_id": "...",
  "timestamp_start": "...",
  "timestamp_end": "...",
  "latency_ms": 3067,
  "model": "gpt-5-codex",
  "call_type": "aresponses",
  "kwargs": { "input": [...] },
  "response_obj": { "output": [...], "usage": {...} },
  "status": "success",
  "usage": { "prompt_tokens": 13637, "completion_tokens": 69 },
  "response_cost": 0.0177
}
```

Secrets (`OPENAI_API_KEY`, `LITELLM_MASTER_KEY`, auth headers) are recursively redacted to `***REDACTED***` before writing.

## Verified run

Task: **SWE-bench Lite `pallets__flask-5063`** — add subdomain/host column to `flask routes`

- 76 LLM calls, 68 shell commands, 363s
- 2.1M prompt tokens + 35K completion tokens = **$0.70**
- Patch: `src/flask/cli.py` +37/-3

Trajectory: `logs/task_001/requests.jsonl` (58MB)  
Human-readable: `logs/task_001/samples/04_conversation_readable.txt`

## Limitations

- Captures LiteLLM callback-level payloads, not raw HTTP bytes
- Streaming responses are aggregated before logging (no per-chunk deltas)
- No SWE-bench grading, no parallel rollout
- Codex `multi_agent_v1` subagent tool is available but not triggered on simple tasks
