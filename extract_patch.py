#!/usr/bin/env python3
"""Standardize each agent's final code change into model.patch + patch_meta.json.

Why: grading must apply the agent's CODE change onto a clean base — not cp -a the
whole working tree (which drags in temp files, test edits, and stray state). This
script produces a canonical diff-against-base and classifies its quality, so the
harness can tell "model solved it wrong" apart from "agent wrote no patch".

For each task it writes:
  logs/<id>/model.patch       # git diff base..worktree (incl. new files)
  logs/<id>/patch_meta.json   # {has_patch, patch_bytes, modified_files,
                              #  modified_test_files, untracked_files,
                              #  has_untracked_files, patch_status}

patch_status: empty_patch / modified_tests / has_patch.
(invalid_patch is set authoritatively by grade_approx.py, which actually applies
model.patch onto a pristine base.)

Usable as a CLI or imported (run_batch.py calls extract_one()).

Usage:
    python3 extract_patch.py
    python3 extract_patch.py --ids sympy__sympy-21171
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TASKS_PATH = REPO_ROOT / "dataset" / "tasks.jsonl"
WORKSPACE_ROOT = REPO_ROOT / "workspace"
LOGS_ROOT = REPO_ROOT / "logs"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def is_test_file(path: str) -> bool:
    """Heuristic: does this path look like a test file (not product code)?"""
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base == "conftest.py":
        return True
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    parts = p.split("/")
    return any(seg in ("tests", "test", "testing") for seg in parts)


def _diff_against_base(ws: Path, base_commit: str | None) -> tuple[str, list[str]]:
    """Return (patch_text, untracked_files). Captures committed + uncommitted +
    new files via intent-to-add, diffed against base_commit (fallback HEAD)."""
    # Record untracked files before touching the index.
    st = _run(["git", "status", "--porcelain"], ws)
    untracked = [
        ln[3:].strip()
        for ln in st.stdout.splitlines()
        if ln.startswith("?? ")
    ]
    # intent-to-add makes new files show up in `git diff`.
    _run(["git", "add", "-A", "-N"], ws)
    try:
        target = base_commit if base_commit else "HEAD"
        diff = _run(["git", "diff", target], ws)
        patch_text = diff.stdout
    finally:
        # Restore the index so the workspace is left untouched.
        _run(["git", "reset", "-q"], ws)
    return patch_text, untracked


def _modified_files(patch_text: str) -> list[str]:
    files = []
    for ln in patch_text.splitlines():
        if ln.startswith("+++ b/"):
            files.append(ln[len("+++ b/"):].strip())
        elif ln.startswith("--- a/") and ln.endswith(" b/dev/null"):
            pass
    # Also catch pure deletions / renames via the "diff --git a/x b/y" header.
    for ln in patch_text.splitlines():
        if ln.startswith("diff --git "):
            # diff --git a/<x> b/<y>
            try:
                _, _, a, b = ln.split(" ", 3)
                fa = a[2:] if a.startswith("a/") else a
                fb = b[2:] if b.startswith("b/") else b
                for f in (fa, fb):
                    if f and f != "dev/null" and f not in files:
                        files.append(f)
            except ValueError:
                pass
    return sorted(set(f for f in files if f and f != "dev/null"))


def classify(patch_bytes: int, modified_product_files: list[str],
             modified_test_files: list[str]) -> str:
    if patch_bytes == 0:
        return "empty_patch"
    # No product-code change at all (e.g. only a stray junk/untracked file, or
    # only test edits) — effectively no fix from a grading standpoint.
    if not modified_product_files:
        return "modified_tests" if modified_test_files else "empty_patch"
    if modified_test_files:
        return "modified_tests"
    return "has_patch"


def extract_one(instance_id: str, base_commit: str | None = None,
                workspace_dir: Path | None = None,
                log_dir: Path | None = None) -> dict:
    """Extract model.patch + patch_meta.json for one task. Returns patch_meta."""
    ws = (workspace_dir or (WORKSPACE_ROOT / instance_id)).resolve()
    out_dir = (log_dir or (LOGS_ROOT / instance_id))
    out_dir.mkdir(parents=True, exist_ok=True)

    if not (ws / ".git").exists():
        meta = {"instance_id": instance_id, "has_patch": False, "patch_bytes": 0,
                "modified_files": [], "modified_test_files": [], "untracked_files": [],
                "has_untracked_files": False, "patch_status": "empty_patch",
                "error": f"no git repo at {ws}"}
        (out_dir / "patch_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    patch_text, untracked = _diff_against_base(ws, base_commit)
    (out_dir / "model.patch").write_text(patch_text, encoding="utf-8")

    modified = _modified_files(patch_text)
    modified_tests = [f for f in modified if is_test_file(f)]
    modified_product = [f for f in modified if not is_test_file(f)]
    patch_bytes = len(patch_text.encode("utf-8"))
    status = classify(patch_bytes, modified_product, modified_tests)

    meta = {
        "instance_id": instance_id,
        "has_patch": status != "empty_patch",
        "patch_bytes": patch_bytes,
        "modified_files": modified,
        "modified_product_files": modified_product,
        "modified_test_files": modified_tests,
        "untracked_files": untracked,
        "has_untracked_files": bool(untracked),
        "patch_status": status,
    }
    (out_dir / "patch_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset")
    args = ap.parse_args()

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}

    tasks = load_tasks(ids_filter)
    for t in tasks:
        meta = extract_one(t["instance_id"], base_commit=t.get("base_commit"))
        print(f"[extract_patch] {t['instance_id']}: status={meta['patch_status']} "
              f"bytes={meta['patch_bytes']} files={len(meta['modified_files'])} "
              f"test_files={len(meta['modified_test_files'])} "
              f"untracked={len(meta['untracked_files'])}")
    print(f"[extract_patch] done for {len(tasks)} task(s).")


if __name__ == "__main__":
    main()
