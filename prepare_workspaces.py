#!/usr/bin/env python3
"""Prepare per-task workspaces, public task descriptions, and eval patches.

For each task in dataset/tasks.jsonl:
  * clone the repo into workspace/<id> and checkout base_commit
    (if it already exists, verify HEAD == base_commit; --force-reset to fix)
  * write task_public/<id>/task.md  (agent-visible; problem_statement only by
    default; --reveal-test-names appends the FAIL_TO_PASS names)
  * write eval/<id>/test.patch      (grade-only; NEVER mounted to the agent)

Python (not bash): JSONL parsing is brittle in shell, and we need precise HEAD
verification.

Usage:
    python3 prepare_workspaces.py
    python3 prepare_workspaces.py --ids sympy__sympy-21171 --force-reset
    python3 prepare_workspaces.py --reveal-test-names
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
WORKSPACE_ROOT = REPO_ROOT / "workspace"
TASK_PUBLIC_ROOT = REPO_ROOT / "task_public"
EVAL_ROOT = REPO_ROOT / "eval"

# GitHub clone URL by repo slug.
def clone_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


def git_head(repo_dir: Path) -> str | None:
    try:
        return run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def preflight() -> None:
    if shutil.which("git") is None:
        sys.exit("ERROR: git not found on PATH.")
    # Disk warning (best-effort).
    total, used, free = shutil.disk_usage(REPO_ROOT)
    free_gib = free / (1024**3)
    print(f"[prepare] disk free: {free_gib:.1f} GiB")
    if free_gib < 2:
        print("[prepare] WARNING: low disk; sympy clone alone is ~200MB.")


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


def prepare_repo(task: dict, force_reset: bool) -> None:
    iid = task["instance_id"]
    repo = task["repo"]
    base = task["base_commit"]
    repo_dir = WORKSPACE_ROOT / iid

    if repo_dir.exists():
        head = git_head(repo_dir)
        dirty = bool(run(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip())
        if head == base and not dirty:
            print(f"[prepare] {iid}: already at base {base[:10]} and clean, skip")
            return
        if head == base and dirty:
            # HEAD is correct but the working tree carries a prior agent's edits;
            # under --force-reset, scrub it back to a pristine base.
            if not force_reset:
                sys.exit(
                    f"ERROR: {iid}: at base but working tree is dirty "
                    f"(prior run's changes). Pass --force-reset to scrub workspace/{iid}."
                )
            run(["git", "reset", "--hard", base], cwd=repo_dir)
            run(["git", "clean", "-fdx"], cwd=repo_dir)
            print(f"[prepare] {iid}: scrubbed dirty tree back to base {base[:10]} OK")
            return
        if not force_reset:
            sys.exit(
                f"ERROR: {iid}: HEAD={head} != base={base}. "
                f"Pass --force-reset to reset workspace/{iid}."
            )
        print(f"[prepare] {iid}: HEAD={head[:10] if head else None} != base; --force-reset")
        run(["git", "fetch", "--all", "--tags"], cwd=repo_dir, check=False)
        run(["git", "reset", "--hard", base], cwd=repo_dir)
        run(["git", "clean", "-fdx"], cwd=repo_dir)
        head = git_head(repo_dir)
        if head != base:
            sys.exit(f"ERROR: {iid}: reset failed, HEAD={head}")
        print(f"[prepare] {iid}: reset to base {base[:10]} OK")
        return

    print(f"[prepare] {iid}: cloning {repo} ...")
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", clone_url(repo), str(repo_dir)])
    # Checkout exact base commit (detached HEAD).
    try:
        run(["git", "checkout", "-q", base], cwd=repo_dir)
    except subprocess.CalledProcessError:
        # Shallow clones may miss the commit; fetch it explicitly.
        run(["git", "fetch", "--all", "--tags"], cwd=repo_dir, check=False)
        run(["git", "checkout", "-q", base], cwd=repo_dir)
    head = git_head(repo_dir)
    if head != base:
        sys.exit(f"ERROR: {iid}: checkout failed, HEAD={head} != base={base}")
    print(f"[prepare] {iid}: checked out base {base[:10]} OK")


def write_task_md(task: dict, reveal_test_names: bool) -> None:
    iid = task["instance_id"]
    out_dir = TASK_PUBLIC_ROOT / iid
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [
        f"# Task: {iid}",
        "",
        f"Repository: `{task['repo']}` @ `{task['base_commit']}`",
        "",
        "## Problem statement",
        "",
        task["problem_statement"].strip(),
        "",
    ]
    if reveal_test_names:
        parts += [
            "## Tests that must pass",
            "",
            "The following tests are expected to pass after your fix:",
            "",
        ]
        parts += [f"- `{name}`" for name in task["fail_to_pass"]]
        parts += [""]
    (out_dir / "task.md").write_text("\n".join(parts), encoding="utf-8")
    print(f"[prepare] {iid}: wrote task_public/{iid}/task.md "
          f"({'with' if reveal_test_names else 'no'} test names)")


def write_eval_patch(task: dict) -> None:
    iid = task["instance_id"]
    out_dir = EVAL_ROOT / iid
    out_dir.mkdir(parents=True, exist_ok=True)
    patch = task["test_patch"]
    if not patch.endswith("\n"):
        patch += "\n"
    (out_dir / "test.patch").write_text(patch, encoding="utf-8")
    print(f"[prepare] {iid}: wrote eval/{iid}/test.patch ({len(patch)} bytes)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset of instance_ids")
    ap.add_argument("--force-reset", action="store_true",
                    help="git reset --hard + clean -fdx if workspace HEAD != base")
    ap.add_argument("--reveal-test-names", action="store_true",
                    help="append FAIL_TO_PASS names to task.md (easier, less realistic)")
    args = ap.parse_args()

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}

    preflight()
    tasks = load_tasks(ids_filter)
    print(f"[prepare] preparing {len(tasks)} task(s)")

    for task in tasks:
        prepare_repo(task, args.force_reset)
        write_task_md(task, args.reveal_test_names)
        write_eval_patch(task)

    print(f"[prepare] done. workspace/ task_public/ eval/ populated for {len(tasks)} task(s).")
    print("[prepare] reminder: eval/ is grade-only; run_batch.py must NOT mount it.")


if __name__ == "__main__":
    main()
