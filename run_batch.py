#!/usr/bin/env python3
"""Concurrent agent-rollout scheduler (the harness core).

Runs each task in its own Docker container (image built by Dockerfile), all
through the same LiteLLM-logging entrypoint (run_task.sh). Containers run
concurrently via a thread pool. The agent only ever sees task_public/<id>/task.md
and workspace/<id>; the eval/ patches are NEVER mounted here.

Per task we write logs/<id>/run_meta.json as the single source of truth
(task_hash / prompt_hash / model / image_id / sandbox / status / times). The
host aggregates logs/batch_results.jsonl single-threaded after all finish.

Usage:
    python3 run_batch.py --max-workers 3 --timeout 1800
    python3 run_batch.py --ids sympy__sympy-21171 --force
    python3 run_batch.py --rerun-failed
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import extract_patch  # sibling module; standardizes model.patch + patch_meta.json

REPO_ROOT = Path(__file__).resolve().parent
TASKS_PATH = REPO_ROOT / "dataset" / "tasks.jsonl"
WORKSPACE_ROOT = REPO_ROOT / "workspace"
TASK_PUBLIC_ROOT = REPO_ROOT / "task_public"
LOGS_ROOT = REPO_ROOT / "logs"

DEFAULT_IMAGE = "one-task-litellm-codex-logger:latest"
DEFAULT_MODEL = "gpt-5-codex"
DEFAULT_SANDBOX = "danger-full-access"

PROMPT_TEMPLATE = (
    "Read /task/task.md for the coding task. The repository to fix is at "
    "/workspace (already checked out at the right commit). Inspect the codebase, "
    "implement the fix, and run the project's own tests to verify.\n\n"
    "This is a large codebase: consider using the multi_agent_v1 tool to spawn a "
    "sub-agent to explore the repo and locate the relevant code, so the main "
    "thread can focus on implementing and testing the fix.\n\n"
    "REQUIREMENTS (hard constraints):\n"
    "- You MUST implement a concrete code fix. Do not stop with analysis or ask "
    "for clarification or confirmation.\n"
    "- Make your best engineering decision and leave the source tree modified "
    "with your fix.\n"
    "- Do NOT edit test files; fix the underlying product code.\n"
    "- Run targeted verification if feasible before finishing."
)


def safe_id(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", instance_id)


def sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def preflight(image: str, min_disk_gb: float) -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("ERROR: OPENAI_API_KEY not set in this shell (LiteLLM needs it).")
    # Disk guard: danger-full-access agents can balloon Docker's disk (pip/build
    # output + resent history). Refuse to start when free space is low.
    import shutil
    free_gb = shutil.disk_usage(REPO_ROOT).free / (1024 ** 3)
    if free_gb < min_disk_gb:
        sys.exit(
            f"ERROR: only {free_gb:.1f}GiB free (< {min_disk_gb}GiB required). "
            "Free disk first (docker data + large files); aborting to avoid ENOSPC.")
    print(f"[run_batch] disk free: {free_gb:.1f}GiB (>= {min_disk_gb}GiB)")
    info = run(["docker", "info"])
    if info.returncode != 0:
        sys.exit(f"ERROR: docker not available:\n{info.stderr.strip()}")
    insp = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    if insp.returncode != 0:
        sys.exit(
            f"ERROR: image '{image}' not found. Build it first "
            f"(docker build -t {image} .).\n{insp.stderr.strip()}"
        )
    image_id = insp.stdout.strip()
    print(f"[run_batch] image {image} -> {image_id[:19]}")
    return image_id


def load_tasks(tasks_path: Path, ids_filter: set[str] | None) -> list[dict]:
    if not tasks_path.exists():
        sys.exit(f"ERROR: {tasks_path} not found. Run fetch_tasks.py first.")
    tasks = []
    for ln in tasks_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        t = json.loads(ln)
        if ids_filter and t["instance_id"] not in ids_filter:
            continue
        tasks.append(t)
    if ids_filter:
        missing = ids_filter - {t["instance_id"] for t in tasks}
        if missing:
            sys.exit(f"ERROR: --ids not in tasks.jsonl: {sorted(missing)}")
    return tasks


def task_hash(task: dict) -> str:
    payload = json.dumps(
        {"instance_id": task["instance_id"], "base_commit": task["base_commit"]},
        sort_keys=True,
    )
    return sha256_short(payload)


def read_run_meta(iid: str) -> dict | None:
    p = LOGS_ROOT / iid / "run_meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def should_skip(task: dict, prompt: str, model: str, image_id: str,
                force: bool, rerun_failed: bool) -> bool:
    if force:
        return False
    meta = read_run_meta(task["instance_id"])
    if not meta:
        return False
    if rerun_failed:
        # Rerun failed/timeout/interrupted AND success-but-no-real-fix
        # (empty_patch / invalid_patch). Skip only genuinely-good successes.
        if meta.get("status") != "success":
            return False
        if meta.get("patch_status") in ("empty_patch", "invalid_patch"):
            return False
        return True
    # Default resume: skip success with matching identity.
    return (
        meta.get("status") == "success"
        and meta.get("task_hash") == task_hash(task)
        and meta.get("prompt_hash") == sha256_short(prompt)
        and meta.get("model") == model
        and meta.get("image_id") == image_id
    )


def docker_rm_f(name: str) -> None:
    run(["docker", "rm", "-f", name])


def disk_monitor(abort_event: threading.Event, critical_gb: float,
                 interval: float = 15.0) -> None:
    """Watchdog: logs are kept 100% raw (no truncation), so a pathological giant
    tool output resent each turn could still fill the disk mid-run. This polls
    free space and, if it drops below the critical line, kills all rollout
    containers and signals abort — preventing a machine-freezing ENOSPC.
    """
    while not abort_event.wait(interval):
        try:
            free_gb = shutil.disk_usage(REPO_ROOT).free / (1024 ** 3)
        except Exception:  # noqa: BLE001
            continue
        if free_gb < critical_gb:
            print(f"\n[run_batch] ⚠ CRITICAL: disk free {free_gb:.1f}GiB < "
                  f"{critical_gb}GiB — aborting batch, killing containers.")
            abort_event.set()
            names = run(["docker", "ps", "--filter", "name=agentic-rollout-",
                         "--format", "{{.Names}}"]).stdout.split()
            for n in names:
                docker_rm_f(n)
            return


def write_run_meta(iid: str, meta: dict) -> None:
    (LOGS_ROOT / iid).mkdir(parents=True, exist_ok=True)
    (LOGS_ROOT / iid / "run_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def clean_task_outputs(log_dir: Path) -> None:
    """Remove stale per-task artifacts before a (re)run.

    The LiteLLM callback APPENDS to requests.jsonl, so without this a rerun would
    interleave old + new records (and double the token totals). We clear every
    file this run regenerates; run_meta.json is rewritten immediately after.
    """
    import shutil
    for name in ("requests.jsonl", "agent_stdout.log", "agent_stderr.log",
                 "litellm_stdout.log", "litellm_stderr.log", "diff.patch",
                 "final_result.json", "model.patch", "patch_meta.json",
                 "session_graph.json", "rollouts.jsonl",
                 "eval_approx_result.json", "eval_official_result.json",
                 "eval_apply.log", "eval_model_apply.log",
                 "eval_install_stdout.log", "eval_install_pytest.log",
                 "docker_stdout.log", "docker_stderr.log",
                 "grade_docker_stdout.log", "grade_docker_stderr.log"):
        p = log_dir / name
        if p.exists():
            p.unlink()
    tests_dir = log_dir / "eval_tests"
    if tests_dir.is_dir():
        shutil.rmtree(tests_dir, ignore_errors=True)


def run_one_task(task: dict, args, image_id: str, prompt: str,
                 started_iso: str, abort_event: threading.Event | None = None) -> dict:
    iid = task["instance_id"]
    if abort_event is not None and abort_event.is_set():
        return {"instance_id": iid, "status": "aborted_disk",
                "error": "batch aborted (low disk) before this task started"}
    sid = safe_id(iid)
    container = f"agentic-rollout-{sid}"
    log_dir = LOGS_ROOT / iid
    log_dir.mkdir(parents=True, exist_ok=True)
    # Fresh start: the callback appends, so clear stale artifacts first.
    clean_task_outputs(log_dir)

    ws = (WORKSPACE_ROOT / iid).resolve()
    task_md = (TASK_PUBLIC_ROOT / iid / "task.md").resolve()
    logs_abs = LOGS_ROOT.resolve()

    if not ws.exists():
        return {"instance_id": iid, "status": "failed",
                "error": f"workspace missing: {ws}"}
    if not task_md.exists():
        return {"instance_id": iid, "status": "failed",
                "error": f"task.md missing: {task_md}"}

    meta = {
        "instance_id": iid,
        "task_hash": task_hash(task),
        "prompt_hash": sha256_short(prompt),
        "model": args.model,
        "image_id": image_id,
        "sandbox": args.sandbox,
        "container": container,
        "started_at": started_iso,
        "ended_at": None,
        "status": "running",
    }
    write_run_meta(iid, meta)

    # Stale-container guard from a previous interrupted run.
    docker_rm_f(container)

    cmd = [
        "docker", "run", "--rm", "--name", container,
        "-e", f"TASK_ID={iid}",
        "-e", f"SESSION_ID={sid}",
        "-e", f"CODEX_MODEL={args.model}",
        "-e", f"CODEX_SANDBOX={args.sandbox}",
        "-e", f"TASK_PROMPT={prompt}",
        "-e", f"OPENAI_API_KEY={os.environ['OPENAI_API_KEY']}",
        "-e", "LITELLM_MASTER_KEY=sk-local-dev",
        "-v", f"{ws}:/workspace",
        "-v", f"{task_md}:/task/task.md:ro",
        "-v", f"{logs_abs}:/logs",
        image_id,
    ]

    docker_stdout = log_dir / "docker_stdout.log"
    docker_stderr = log_dir / "docker_stderr.log"

    t0 = time.time()
    status = "failed"
    exit_code = None
    timed_out = False
    print(f"[run_batch] {iid}: docker run (container={container})")
    try:
        with open(docker_stdout, "w") as so, open(docker_stderr, "w") as se:
            proc = subprocess.run(
                cmd, stdout=so, stderr=se, timeout=args.timeout
            )
        exit_code = proc.returncode
        status = "success" if exit_code == 0 else "failed"
    except subprocess.TimeoutExpired:
        timed_out = True
        status = "timeout"
        with open(docker_stderr, "a") as se:
            se.write(f"\n[run_batch] TIMEOUT after {args.timeout}s; docker rm -f {container}\n")
        print(f"[run_batch] {iid}: TIMEOUT after {args.timeout}s")
    finally:
        # subprocess timeout only kills the docker *client*; force-remove the
        # container so in-container Codex/LiteLLM are actually torn down. Also a
        # best-effort cleanup when --rm didn't fire.
        docker_rm_f(container)

    duration = round(time.time() - t0, 1)
    if abort_event is not None and abort_event.is_set() and status != "success":
        status = "aborted_disk"

    # Standardize the agent's change into model.patch + patch_meta.json, and
    # fold patch_status into run_meta so "agent wrote no fix" is distinguishable
    # from "agent ran fine" even when the container exited 0.
    patch_status = None
    try:
        pmeta = extract_patch.extract_one(iid, base_commit=task.get("base_commit"))
        patch_status = pmeta.get("patch_status")
    except Exception as e:  # noqa: BLE001
        print(f"[run_batch] {iid}: extract_patch failed: {e}")

    meta.update({
        "ended_at": _iso_from_epoch(t0 + duration),
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "patch_status": patch_status,
    })
    write_run_meta(iid, meta)
    print(f"[run_batch] {iid}: status={status} patch={patch_status} duration={duration}s")
    return {
        "instance_id": iid,
        "status": status,
        "patch_status": patch_status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "container": container,
    }


def _iso_from_epoch(epoch: float) -> str:
    # Avoid Date.now-style nondeterminism concerns; this is host-side logging.
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch).astimezone().isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", default=str(TASKS_PATH), help="tasks.jsonl path")
    ap.add_argument("--ids", help="comma-separated subset")
    ap.add_argument("--max-workers", type=int, default=3)
    ap.add_argument("--min-disk-gb", type=float, default=20.0,
                    help="refuse to start if free disk is below this")
    ap.add_argument("--critical-disk-gb", type=float, default=8.0,
                    help="abort mid-run (kill containers) if free disk drops below this")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--sandbox", default=DEFAULT_SANDBOX)
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--timeout", type=int, default=1800, help="per-task seconds")
    ap.add_argument("--force", action="store_true", help="rerun everything")
    ap.add_argument("--rerun-failed", action="store_true",
                    help="rerun only failed/timeout tasks")
    args = ap.parse_args()

    tasks_path = Path(args.tasks).resolve()

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}

    image_id = preflight(args.image, args.min_disk_gb)
    tasks = load_tasks(tasks_path, ids_filter)
    prompt = PROMPT_TEMPLATE
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)

    to_run, skipped = [], []
    for t in tasks:
        if should_skip(t, prompt, args.model, image_id, args.force, args.rerun_failed):
            skipped.append(t["instance_id"])
        else:
            to_run.append(t)

    if skipped:
        print(f"[run_batch] skipping {len(skipped)} already-done: {skipped}")
    if not to_run:
        print("[run_batch] nothing to run. Use --force to rerun.")
        return

    print(f"[run_batch] running {len(to_run)} task(s), max_workers={args.max_workers}, "
          f"timeout={args.timeout}s, model={args.model}, sandbox={args.sandbox}")

    started_iso = _iso_from_epoch(time.time())

    # Write run_config.json up front (source of truth for this batch's params).
    run_config = {
        "model": args.model,
        "sandbox": args.sandbox,
        "image": args.image,
        "image_id": image_id,
        "prompt_hash": sha256_short(prompt),
        "prompt": prompt,
        "timeout": args.timeout,
        "max_workers": args.max_workers,
        "started_at": started_iso,
        "tasks": [t["instance_id"] for t in to_run],
    }
    (LOGS_ROOT / "run_config.json").write_text(
        json.dumps(run_config, indent=2), encoding="utf-8"
    )

    # Runtime disk watchdog (logs are kept full; this prevents an ENOSPC freeze).
    abort_event = threading.Event()
    monitor = threading.Thread(
        target=disk_monitor, args=(abort_event, args.critical_disk_gb), daemon=True)
    monitor.start()

    # Incrementally append each result to batch_results.jsonl as tasks finish
    # (main thread only — no write race). Truncate first so a rerun is clean.
    batch_path = LOGS_ROOT / "batch_results.jsonl"
    results: list[dict] = []
    try:
        with open(batch_path, "w", encoding="utf-8") as bf, \
                futures.ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            fut_map = {
                ex.submit(run_one_task, t, args, image_id, prompt, started_iso, abort_event): t["instance_id"]
                for t in to_run
            }
            for fut in futures.as_completed(fut_map):
                iid = fut_map[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = {"instance_id": iid, "status": "failed", "error": str(e)}
                    print(f"[run_batch] {iid}: EXCEPTION {e}")
                results.append(r)
                bf.write(json.dumps(r, ensure_ascii=False) + "\n")
                bf.flush()
    finally:
        abort_event.set()  # stop the monitor thread

    if any(r.get("status") == "aborted_disk" for r in results):
        print("[run_batch] ⚠ batch aborted due to low disk — free space and rerun "
              "with --rerun-failed.")

    ok = sum(1 for r in results if r["status"] == "success")
    print(f"\n[run_batch] DONE: {ok}/{len(results)} success")
    for r in sorted(results, key=lambda x: x["instance_id"]):
        print(f"    {r['instance_id']:30s} {r['status']:8s} "
              f"{r.get('duration_seconds','?')}s")
    print(f"[run_batch] wrote {batch_path} and run_config.json")


if __name__ == "__main__":
    main()
