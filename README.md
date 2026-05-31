# agentic-rl-rollout

Capture coding-agent **trajectories** (every LLM API call) and grade their
correctness, by putting a **LiteLLM proxy** in front of Codex CLI inside Docker.
Built for collecting RL rollouts / failure analysis on SWE-bench-style tasks.

```
Codex CLI ──► LiteLLM Proxy (127.0.0.1:4000) ──► OpenAI API
                    │  custom callback
                    ▼
              logs/<id>/requests.jsonl   (one JSON record per LLM call)
```

Two keys, distinct roles (the proxy isolates them):
- `OPENAI_API_KEY` — used **only** by LiteLLM to call OpenAI. Codex never sees it.
- `LITELLM_MASTER_KEY` — local proxy key Codex authenticates with (`sk-local-dev`). Redacted from logs.

---

## Quickstart (single task)

```bash
docker build -t one-task-litellm-codex-logger:latest .
bash setup_workspace.sh                      # clones flask @ the SWE-bench base commit

docker run --rm \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e TASK_ID="task_001" -e CODEX_MODEL="gpt-5-codex" \
  -e CODEX_SANDBOX="danger-full-access" \
  -e TASK_PROMPT="Read /task/task.md and solve the task in /workspace." \
  -v "$PWD/workspace/flask:/workspace" -v "$PWD/logs:/logs" -v "$PWD/task:/task:ro" \
  one-task-litellm-codex-logger:latest
```

`logs/task_001/` holds the committed example trajectory (76 calls, 2.1M tokens,
$0.70; its `session_graph.json` / `rollouts.jsonl` show the structured-artifact
format below).

---

## Batch pipeline (multiple tasks, concurrent, graded)

Run many SWE-bench Lite tasks concurrently, standardize each agent patch, grade
correctness (approximate + optional official), reconstruct the per-session
request graph, and aggregate a report with a failure taxonomy.

```bash
# 0. one-time: build image, have OPENAI_API_KEY in your shell, Docker running
docker build -t one-task-litellm-codex-logger:latest .

# 1. pick tasks  -> dataset/tasks.jsonl   (defaults to 3 example tasks)
python3 fetch_tasks.py --ids sympy__sympy-21171,pytest-dev__pytest-7490,pylint-dev__pylint-7080

# 2. clone repos @ base_commit, write agent-visible task.md + grade-only test.patch
python3 prepare_workspaces.py            # --reveal-test-names to put F2P names in task.md

# 3. run agents concurrently (collects trajectories, writes model.patch + patch_meta)
python3 run_batch.py --max-workers 3 --timeout 1800

# 4. approximate correctness grading (decoupled, rerunnable)
python3 grade_approx.py --max-workers 3 --pass-to-pass-limit 20

# 5. (optional) official SWE-bench grading — authoritative; needs `pip install swebench` + ~30GB disk
python3 grade_official.py

# 6. reconstruct request graph + RL rollouts per task
python3 build_rollouts.py

# 7. aggregate: tokens/cost, subagent stats, resolved + failure taxonomy
python3 analyze_trajectories.py
```

`fetch_tasks.py` reads SWE-bench Lite from the HF dataset-viewer cache at
`/tmp/swe_lite_p*.json` (download those, or adapt the loader).

### Artifacts per task (`logs/<instance_id>/`)

| File | Produced by | Contents |
|---|---|---|
| `requests.jsonl` | run_batch | raw LLM call log (+ derived `seq`, `function_calls`, `input_item_ids`, …) |
| `run_meta.json` | run_batch | agent status, patch_status, timings (single source of truth) |
| `model.patch` / `patch_meta.json` | extract_patch | standardized diff-vs-base + quality (`has_patch`/`empty_patch`/`modified_tests`) |
| `eval_approx_result.json` | grade_approx | approximate resolved + taxonomy + per-test results |
| `eval_official_result.json` | grade_official | authoritative resolved (or a `skipped_*` verdict) |
| `session_graph.json` | build_rollouts | nodes/edges/threads; `subagent_detected` vs `subagent_reconstructed`, `graph_confidence` |
| `rollouts.jsonl` | build_rollouts | one RL-consumable row per thread (main + each sub-agent) |

`logs/summary.json` + the Markdown table from `analyze_trajectories.py` join it all.

### Failure taxonomy (so `0/N resolved` is explained, not opaque)

```
agent_status : success / failed / timeout / aborted_disk
patch_status : has_patch / empty_patch / modified_tests / invalid_patch
eval_status  : ok / install_failed / test_patch_failed / collection_failed /
               baseline_invalid / suspect_env / timeout
resolved     : PASS / FAIL / unknown            (official > approx > unknown)
failure_type : model_wrong / env_failed / no_patch / harness_failed /
               timeout / policy_violation
```

The approximate grader is honest by design: it builds a **pristine base**,
verifies each FAIL_TO_PASS test actually fails *before* the fix (baseline check),
resolves bare test names to exact nodeids (no `pytest -k` substring traps), and
flags a broken eval env (`PASS_TO_PASS 0/N → suspect_env → unknown`) instead of
mislabeling it `FAIL`. It is **not** the official harness — use `grade_official.py`
for the authoritative score.

---

## Sub-agent trajectories

Codex 0.135 exposes a multi-agent toolset (`spawn_agent` / `wait_agent` /
`close_agent`). When triggered, the sub-agent's own LLM calls land in the **same**
`requests.jsonl`, interleaved. Codex is stateless on the responses wire (it
resends the full `input` each turn), so `build_rollouts.py` separates threads by
**input-prefix containment** and reports `subagent_detected` (reliable, from the
spawn calls) separately from `subagent_reconstructed` (approximate, since a
sub-agent's calls carry no parent id).

---

## Reproduction notes / gotchas

- **Disk.** Agents run with `danger-full-access` and install deps inside the
  container; the Docker VM disk (`Docker.raw` on macOS) grows fast. `run_batch.py`
  refuses to start below `--min-disk-gb` (20) and aborts mid-run below
  `--critical-disk-gb` (8), killing containers to avoid an ENOSPC freeze. Keep
  ~30GB free; `docker builder prune -af` reclaims build cache between rebuilds.
- **LiteLLM log bloat.** LiteLLM nests retry history (`previous_models`) into
  every call's `litellm_params`, growing ~5× per turn (→ 100s of MB/record). The
  callback prunes that internal key — trajectory fields (`input` / `response_obj`
  / `tools` / `usage`) are kept **100% untruncated**.
- **Network (China).** Dockerfile uses Tsinghua PyPI + npmmirror; Codex's native
  binary is injected explicitly (`@openai/codex-<platform>`). For registry pulls
  behind Clash, enable TUN mode.
- **No test leakage.** `eval/<id>/test.patch` is mounted only to grade containers,
  never to the agent — `task.md` is the only thing the agent sees.
- **Secrets** (`OPENAI_API_KEY`, master key, auth headers, bearer/sk- tokens) are
  recursively redacted to `***REDACTED***` before any record is written.

## Limitations

- Captures LiteLLM callback payloads, not raw HTTP bytes; streaming is aggregated.
- Approximate grading resolves deps at grade time → version drift vs the official
  per-instance environments (that's what `grade_official.py` is for).
- Sub-agent thread reconstruction is heuristic (`subagent_reconstructed` is
  conservative; the spawn→thread link is by timing, not identity).
