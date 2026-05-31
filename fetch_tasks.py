#!/usr/bin/env python3
"""Extract SWE-bench Lite task definitions into dataset/tasks.jsonl.

The fields FAIL_TO_PASS / PASS_TO_PASS are stored as JSON *strings* in the HF
cache; we json.loads them into real lists. test_patch / patch are plain text.

Cache layout: /tmp/swe_lite_p*.json, each a HF dataset-viewer response:
    {"rows": [{"row_idx": int, "row": {<task fields>}, ...}, ...], ...}

Usage:
    python3 fetch_tasks.py                       # default 3 example tasks
    python3 fetch_tasks.py --ids id1,id2,id3
    python3 fetch_tasks.py --ids-file ids.txt
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Default example tasks (pure-Python repos, avoid compiled deps). Configurable.
DEFAULT_IDS = [
    "sympy__sympy-21171",
    "pytest-dev__pytest-7490",
    "pylint-dev__pylint-7080",
]

# Per-repo install command override for grading (best-effort approximation).
INSTALL_CMD_BY_REPO = {
    "sympy/sympy": "pip install -e .",
    "pytest-dev/pytest": "pip install -e . hypothesis",
    "pylint-dev/pylint": "pip install -e . pytest",
}


def load_cache_rows(cache_glob: str) -> dict[str, dict]:
    """Return {instance_id: row_dict} merged across all cache files."""
    rows: dict[str, dict] = {}
    files = sorted(glob.glob(cache_glob))
    if not files:
        sys.exit(
            f"ERROR: no cache files match {cache_glob}. "
            "Expected /tmp/swe_lite_p*.json (HF dataset-viewer dumps)."
        )
    for f in files:
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"ERROR: failed to read {f}: {e}")
        for wrapper in data.get("rows", []):
            row = wrapper.get("row", {})
            iid = row.get("instance_id")
            if iid:
                rows[iid] = row
    return rows


def _as_list(value) -> list:
    """FAIL_TO_PASS / PASS_TO_PASS are JSON-array strings in the cache."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        return json.loads(value)
    return []


def build_task(row: dict) -> dict:
    repo = row["repo"]
    return {
        "instance_id": row["instance_id"],
        "repo": repo,
        "base_commit": row["base_commit"],
        "problem_statement": row.get("problem_statement", ""),
        "test_patch": row.get("test_patch", ""),
        "fail_to_pass": _as_list(row.get("FAIL_TO_PASS")),
        "pass_to_pass": _as_list(row.get("PASS_TO_PASS")),
        "install_cmd": INSTALL_CMD_BY_REPO.get(repo, ""),
        "version": row.get("version", ""),
        "environment_setup_commit": row.get("environment_setup_commit", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated instance_ids")
    ap.add_argument("--ids-file", help="file with one instance_id per line")
    ap.add_argument(
        "--dataset",
        default="swe-lite",
        choices=["swe-lite"],
        help="only swe-lite is supported in this step",
    )
    ap.add_argument(
        "--cache-glob",
        default="/tmp/swe_lite_p*.json",
        help="glob for the HF dataset-viewer cache files",
    )
    ap.add_argument(
        "--out",
        default=str(REPO_ROOT / "dataset" / "tasks.jsonl"),
        help="output JSONL path",
    )
    args = ap.parse_args()

    if args.ids and args.ids_file:
        sys.exit("ERROR: pass only one of --ids / --ids-file")

    if args.ids:
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    elif args.ids_file:
        ids = [
            ln.strip()
            for ln in Path(args.ids_file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
    else:
        ids = list(DEFAULT_IDS)
        print(f"[fetch_tasks] no --ids given, using {len(ids)} default example tasks")

    rows = load_cache_rows(args.cache_glob)

    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(
            "ERROR: these instance_ids were not found in the cache:\n  "
            + "\n  ".join(missing)
            + f"\n(cache has {len(rows)} tasks from {args.cache_glob})"
        )

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for iid in ids:
            task = build_task(rows[iid])
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
            print(
                f"[fetch_tasks] {iid}: repo={task['repo']} "
                f"base={task['base_commit'][:10]} "
                f"F2P={len(task['fail_to_pass'])} P2P={len(task['pass_to_pass'])}"
            )

    print(f"[fetch_tasks] wrote {len(ids)} tasks -> {out_path}")
    print(
        "[fetch_tasks] NOTE: tasks.jsonl contains test_patch (eval-side data). "
        "Do not expose it to the agent container."
    )


if __name__ == "__main__":
    main()
