#!/usr/bin/env python3
"""Official SWE-bench grading adapter (authoritative resolved when runnable).

This wraps the official `swebench` harness. It is the AUTHORITATIVE source of
`resolved` (analyze prefers eval_official_result.json over eval_approx_result.json).

It is disk-heavy: the official harness builds/pulls one Docker image per instance
(~2-2.5GB each). A preflight gates on free disk, a working `swebench` install, and
docker. When any precondition fails it writes a skipped verdict per task and exits
0 — it never crashes the pipeline.

Flow when runnable:
  1. build predictions from logs/<id>/model.patch
  2. python -m swebench.harness.run_evaluation --predictions_path ... --run_id ...
  3. parse the report -> eval_official_result.json per task

Usage:
    python3 grade_official.py --min-disk-gb 30          # gated; writes skipped if low
    python3 grade_official.py --ids sympy__sympy-21171 --run-id rollout1
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TASKS_PATH = REPO_ROOT / "dataset" / "tasks.jsonl"
LOGS_ROOT = REPO_ROOT / "logs"
RESULT_NAME = "eval_official_result.json"
DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
MODEL_NAME = "agentic-rollout"


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


def free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


def have_swebench() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("swebench") is not None
    except Exception:  # noqa: BLE001
        return False


def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def write_skipped(tasks: list[dict], status: str, note: str) -> None:
    for t in tasks:
        iid = t["instance_id"]
        d = LOGS_ROOT / iid
        d.mkdir(parents=True, exist_ok=True)
        (d / RESULT_NAME).write_text(json.dumps({
            "task_id": iid, "eval_source": "official",
            "eval_status": status, "resolved": "unknown", "note": note,
        }, indent=2), encoding="utf-8")
    print(f"[grade_official] {status}: {note} ({len(tasks)} task(s) marked skipped)")


def build_predictions(tasks: list[dict], out_path: Path) -> int:
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for t in tasks:
            iid = t["instance_id"]
            mp = LOGS_ROOT / iid / "model.patch"
            patch = mp.read_text(encoding="utf-8") if mp.exists() else ""
            f.write(json.dumps({
                "instance_id": iid,
                "model_name_or_path": MODEL_NAME,
                "model_patch": patch,
            }) + "\n")
            n += 1
    return n


def parse_report_and_write(report: dict, tasks: list[dict]) -> None:
    resolved_ids = set(report.get("resolved_ids", []))
    error_ids = set(report.get("error_ids", []))
    unresolved_ids = set(report.get("unresolved_ids", []))
    for t in tasks:
        iid = t["instance_id"]
        if iid in resolved_ids:
            resolved, status = "PASS", "ok"
        elif iid in unresolved_ids:
            resolved, status = "FAIL", "ok"
        elif iid in error_ids:
            resolved, status = "unknown", "harness_failed"
        else:
            resolved, status = "unknown", "not_in_report"
        (LOGS_ROOT / iid / RESULT_NAME).write_text(json.dumps({
            "task_id": iid, "eval_source": "official",
            "eval_status": status, "resolved": resolved,
        }, indent=2), encoding="utf-8")
        print(f"[grade_official] {iid}: resolved={resolved} ({status})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--run-id", default="rollout")
    ap.add_argument("--max-workers", type=int, default=2)
    ap.add_argument("--min-disk-gb", type=float, default=30.0,
                    help="required free disk before attempting (official images are ~2-2.5GB each)")
    ap.add_argument("--force", action="store_true",
                    help="regrade even if eval_official_result.json exists")
    args = ap.parse_args()

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}
    tasks = load_tasks(ids_filter)

    if not args.force:
        tasks = [t for t in tasks
                 if not (LOGS_ROOT / t["instance_id"] / RESULT_NAME).exists()
                 or json.loads((LOGS_ROOT / t["instance_id"] / RESULT_NAME)
                               .read_text()).get("eval_status", "").startswith("skipped")]
    if not tasks:
        print("[grade_official] nothing to grade (all have official results).")
        return

    # --- preflight gates (write skipped + exit 0 on any failure) ---
    if not have_swebench():
        write_skipped(tasks, "skipped_no_swebench",
                      "pip install swebench to enable official grading")
        return
    if not docker_ok():
        write_skipped(tasks, "skipped_no_docker", "docker not available")
        return
    disk = free_gib(REPO_ROOT)
    if disk < args.min_disk_gb:
        write_skipped(tasks, "skipped_insufficient_disk",
                      f"free {disk:.1f}GiB < required {args.min_disk_gb}GiB "
                      f"(official images ~2-2.5GB/instance)")
        return

    # --- runnable: build predictions and invoke the official harness ---
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    preds = LOGS_ROOT / "predictions.jsonl"
    n = build_predictions(tasks, preds)
    print(f"[grade_official] built {n} predictions -> {preds}")

    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", args.dataset,
        "--predictions_path", str(preds),
        "--max_workers", str(args.max_workers),
        "--run_id", args.run_id,
        "--instance_ids", *[t["instance_id"] for t in tasks],
    ]
    print(f"[grade_official] running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        write_skipped(tasks, "harness_failed",
                      f"run_evaluation exited {proc.returncode}; see console")
        return

    # The harness writes <model>.<run_id>.json in cwd.
    report_path = REPO_ROOT / f"{MODEL_NAME}.{args.run_id}.json"
    if not report_path.exists():
        cands = sorted(REPO_ROOT.glob(f"*{args.run_id}.json"))
        report_path = cands[-1] if cands else None
    if not report_path or not report_path.exists():
        write_skipped(tasks, "harness_failed", "report json not found after run")
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    parse_report_and_write(report, tasks)
    print(f"[grade_official] done; report={report_path.name}")


if __name__ == "__main__":
    main()
