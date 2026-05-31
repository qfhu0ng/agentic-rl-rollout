#!/usr/bin/env python3
"""Approximate SWE-bench grading (decoupled from rollout; rerunnable).

APPROXIMATION, not the official harness: deps are resolved at grade time and may
differ from the official environment. Use eval_official_result.json as the
authoritative score when available; this is a fast, local sanity check.

For each task it starts a grade container (same image) that mounts:
  * workspace/<id>          (read-only; used only for its git history)
  * eval/<id>/test.patch    (read-only)
  * grade_in_container.py   (read-only)
  * logs/                   (writable; reads model.patch, writes eval_approx_result.json)

The container builds a pristine base, installs deps once, runs a baseline phase
(F2P must fail without the fix) then the eval phase (model.patch + test.patch).
See grade_in_container.py.

Usage:
    python3 grade_approx.py --max-workers 3
    python3 grade_approx.py --pass-to-pass-limit all
    python3 grade_approx.py --ids sympy__sympy-21171 --force
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TASKS_PATH = REPO_ROOT / "dataset" / "tasks.jsonl"
WORKSPACE_ROOT = REPO_ROOT / "workspace"
EVAL_ROOT = REPO_ROOT / "eval"
LOGS_ROOT = REPO_ROOT / "logs"
GRADER_SCRIPT = REPO_ROOT / "grade_in_container.py"

DEFAULT_IMAGE = "one-task-litellm-codex-logger:latest"
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
RESULT_NAME = "eval_approx_result.json"


def safe_id(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", instance_id)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def preflight(image: str) -> None:
    if run(["docker", "info"]).returncode != 0:
        sys.exit("ERROR: docker not available.")
    if run(["docker", "image", "inspect", image]).returncode != 0:
        sys.exit(f"ERROR: image '{image}' not found.")
    if not GRADER_SCRIPT.exists():
        sys.exit(f"ERROR: {GRADER_SCRIPT} missing.")


def load_tasks(ids_filter: set[str] | None) -> list[dict]:
    if not TASKS_PATH.exists():
        sys.exit(f"ERROR: {TASKS_PATH} not found. Run fetch_tasks.py first.")
    tasks = []
    for ln in TASKS_PATH.read_text(encoding="utf-8").splitlines():
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


def test_files_from_patch(patch_path: Path) -> list[str]:
    files = []
    for ln in patch_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", ln)
        if m:
            files.append(m.group(1).strip())
    return files


def sample_pass_to_pass(p2p: list[str], limit) -> tuple[list[str], bool]:
    if limit == "all":
        return p2p, False
    n = int(limit)
    if len(p2p) <= n:
        return p2p, False
    return p2p[:n], True  # deterministic head sample (reproducible)


def docker_rm_f(name: str) -> None:
    run(["docker", "rm", "-f", name])


def grade_one(task: dict, args, image: str) -> dict:
    iid = task["instance_id"]
    sid = safe_id(iid)
    container = f"agentic-grade-{sid}"
    log_dir = LOGS_ROOT / iid
    log_dir.mkdir(parents=True, exist_ok=True)

    ws = (WORKSPACE_ROOT / iid).resolve()
    patch = (EVAL_ROOT / iid / "test.patch").resolve()
    logs_abs = LOGS_ROOT.resolve()
    grader = GRADER_SCRIPT.resolve()
    model_patch = log_dir / "model.patch"

    if not ws.exists():
        return {"instance_id": iid, "resolved": "unknown", "error": "workspace missing"}
    if not patch.exists():
        return {"instance_id": iid, "resolved": "unknown", "error": "test.patch missing"}
    if not model_patch.exists():
        print(f"[grade] {iid}: WARNING no model.patch (run extract_patch.py first); "
              "grading will treat it as no-fix")

    p2p, sampled = sample_pass_to_pass(task["pass_to_pass"], args.pass_to_pass_limit)
    tfiles = test_files_from_patch(patch)

    docker_rm_f(container)
    cmd = [
        "docker", "run", "--rm", "--name", container,
        "-e", f"TASK_ID={iid}",
        "-e", f"BASE_COMMIT={task['base_commit']}",
        "-e", f"INSTALL_CMD={task.get('install_cmd') or 'pip install -e .'}",
        "-e", f"FAIL_TO_PASS={json.dumps(task['fail_to_pass'])}",
        "-e", f"PASS_TO_PASS={json.dumps(p2p)}",
        "-e", f"PASS_TO_PASS_SAMPLED={'1' if sampled else '0'}",
        "-e", f"PASS_TO_PASS_LIMIT={args.pass_to_pass_limit}",
        "-e", f"TEST_FILES={json.dumps(tfiles)}",
        "-e", f"PER_TEST_TIMEOUT={args.per_test_timeout}",
        "-e", f"PIP_INDEX_URL={PIP_INDEX}",
        "-e", "PIP_DEFAULT_TIMEOUT=180",
        "-v", f"{ws}:/workspace:ro",
        "-v", f"{patch}:/eval/test.patch:ro",
        "-v", f"{grader}:/grader/grade_in_container.py:ro",
        "-v", f"{logs_abs}:/logs",
        image,
        "bash", "-lc", "python3 /grader/grade_in_container.py",
    ]

    g_stdout = log_dir / "grade_docker_stdout.log"
    g_stderr = log_dir / "grade_docker_stderr.log"
    t0 = time.time()
    print(f"[grade] {iid}: grading (container={container}, "
          f"P2P={'all' if not sampled else f'{len(p2p)} sampled'})")
    try:
        with open(g_stdout, "w") as so, open(g_stderr, "w") as se:
            subprocess.run(cmd, stdout=so, stderr=se, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        with open(g_stderr, "a") as se:
            se.write(f"\n[grade] TIMEOUT after {args.timeout}s; docker rm -f {container}\n")
        docker_rm_f(container)
        # Record a timeout verdict.
        res = {"task_id": iid, "eval_source": "approx", "eval_status": "timeout",
               "resolved": "unknown", "duration_seconds": round(time.time() - t0, 1)}
        (log_dir / RESULT_NAME).write_text(json.dumps(res, indent=2), encoding="utf-8")
        return {"instance_id": iid, "resolved": "unknown", "eval_status": "timeout",
                "duration_seconds": res["duration_seconds"]}
    finally:
        docker_rm_f(container)

    dur = round(time.time() - t0, 1)
    res_path = log_dir / RESULT_NAME
    if not res_path.exists():
        return {"instance_id": iid, "resolved": "unknown",
                "error": "no eval_approx_result.json (see grade_docker_stderr.log)",
                "duration_seconds": dur}
    res = json.loads(res_path.read_text(encoding="utf-8"))
    res["duration_seconds"] = dur
    res_path.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(f"[grade] {iid}: resolved={res['resolved']} "
          f"eval_status={res.get('eval_status')} ({dur}s)")
    return {
        "instance_id": iid,
        "resolved": res["resolved"],
        "eval_status": res.get("eval_status"),
        "test_patch_apply_ok": res.get("test_patch_apply_ok"),
        "install_ok": res.get("install_ok"),
        "fail_to_pass": res.get("fail_to_pass"),
        "pass_to_pass_summary": res.get("pass_to_pass", {}).get("summary"),
        "duration_seconds": dur,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset")
    ap.add_argument("--max-workers", type=int, default=3)
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--pass-to-pass-limit", default="20", help="int (sample N) or 'all'")
    ap.add_argument("--per-test-timeout", type=int, default=300,
                    help="per-test pytest timeout (seconds)")
    ap.add_argument("--timeout", type=int, default=2400, help="per-task container seconds")
    ap.add_argument("--force", action="store_true",
                    help="regrade even if eval_approx_result.json exists")
    args = ap.parse_args()

    if args.pass_to_pass_limit != "all":
        int(args.pass_to_pass_limit)  # validate

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}

    preflight(args.image)
    tasks = load_tasks(ids_filter)

    to_grade = []
    for t in tasks:
        if not args.force and (LOGS_ROOT / t["instance_id"] / RESULT_NAME).exists():
            print(f"[grade] {t['instance_id']}: {RESULT_NAME} exists, skip (--force to regrade)")
            continue
        to_grade.append(t)

    if not to_grade:
        print("[grade] nothing to grade.")
        return

    print(f"[grade] grading {len(to_grade)} task(s), max_workers={args.max_workers}, "
          f"P2P limit={args.pass_to_pass_limit}")

    results = []
    with futures.ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        fut_map = {ex.submit(grade_one, t, args, args.image): t["instance_id"]
                   for t in to_grade}
        for fut in futures.as_completed(fut_map):
            iid = fut_map[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                results.append({"instance_id": iid, "resolved": "unknown", "error": str(e)})
                print(f"[grade] {iid}: EXCEPTION {e}")

    print("\n[grade] DONE:")
    for r in sorted(results, key=lambda x: x["instance_id"]):
        extra = f" ({r['error']})" if r.get("error") else f" [{r.get('eval_status')}]"
        print(f"    {r['instance_id']:30s} {r['resolved']:8s}{extra}")
    n_pass = sum(1 for r in results if r["resolved"] == "PASS")
    print(f"[grade] {n_pass}/{len(results)} resolved=PASS")


if __name__ == "__main__":
    main()
