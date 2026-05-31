#!/usr/bin/env python3
"""Aggregate the structured per-task artifacts into one report.

This no longer re-interprets raw logs for everything; it joins the standardized
products the pipeline now emits:
  run_meta.json          -> agent_status, patch_status, duration
  patch_meta.json        -> patch_status, modified_tests
  session_graph.json     -> subagent_detected/reconstructed, graph_confidence, orphans
  eval_official_result   -> authoritative resolved (preferred)
  eval_approx_result     -> approximate resolved (fallback)
and still scans requests.jsonl only for tokens / cost / redaction audit.

`resolved` priority: official > approx > unknown. A failure taxonomy is derived
per task so 0/N PASS is explained (model_wrong vs env_failed vs no_patch ...).

Outputs Markdown tables to stdout and writes logs/summary.json.

Usage:
    python3 analyze_trajectories.py
    python3 analyze_trajectories.py --ids sympy__sympy-21171
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LOGS_ROOT = REPO_ROOT / "logs"

SECRET_PATTERNS = [
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xoxb-[A-Za-z0-9-]{10,}"),
]
REDACTED = "***REDACTED***"


def load_json(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def pick_eval(official: dict | None, approx: dict | None) -> tuple[str, str, str]:
    """Return (eval_source, eval_status, resolved) honoring official>approx."""
    def is_real(ev):
        return (ev and ev.get("resolved") in ("PASS", "FAIL", "unknown")
                and not str(ev.get("eval_status", "")).startswith("skipped"))
    if official and official.get("resolved") in ("PASS", "FAIL") \
            and not str(official.get("eval_status", "")).startswith("skipped"):
        return "official", official.get("eval_status", "ok"), official["resolved"]
    if is_real(approx):
        return "approx", approx.get("eval_status", "ok"), approx["resolved"]
    if official is not None:  # only a skipped official exists
        return "none", official.get("eval_status", "skipped"), "unknown"
    return "none", "missing", "unknown"


def failure_type(agent_status, patch_status, eval_status, resolved, modified_tests) -> str:
    if resolved == "PASS":
        return "none"
    if agent_status == "timeout" or eval_status == "timeout":
        return "timeout"
    if patch_status == "empty_patch":
        return "no_patch"
    if eval_status in ("install_failed", "suspect_env"):
        return "env_failed"
    if eval_status in ("collection_failed", "baseline_invalid", "test_patch_failed"):
        return "harness_failed"
    if patch_status == "invalid_patch":
        return "harness_failed"
    if modified_tests:
        return "policy_violation"
    if resolved == "FAIL":
        return "model_wrong"
    return "unknown"


def scan_requests(req_path: Path) -> dict:
    """Token/cost totals + redaction audit + record completeness from raw log."""
    s = {"calls": 0, "success_records": 0, "failure_records": 0,
         "missing_usage_records": 0, "redaction_suspect_count": 0,
         "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0}
    if not req_path.exists():
        return s
    with open(req_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            for pat in SECRET_PATTERNS:
                for m in pat.finditer(raw):
                    if REDACTED not in m.group(0):
                        s["redaction_suspect_count"] += 1
            try:
                rec = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            s["calls"] += 1
            if rec.get("status") == "success":
                s["success_records"] += 1
            else:
                s["failure_records"] += 1
            u = rec.get("usage") or {}
            if not u.get("total_tokens"):
                s["missing_usage_records"] += 1
            s["prompt_tokens"] += u.get("prompt_tokens") or 0
            s["completion_tokens"] += u.get("completion_tokens") or 0
            s["total_tokens"] += u.get("total_tokens") or 0
            s["cost"] += rec.get("response_cost") or 0.0
    return s


def analyze_task(task_dir: Path) -> dict:
    iid = task_dir.name
    run_meta = load_json(task_dir / "run_meta.json") or {}
    patch_meta = load_json(task_dir / "patch_meta.json") or {}
    graph = load_json(task_dir / "session_graph.json") or {}
    official = load_json(task_dir / "eval_official_result.json")
    approx = load_json(task_dir / "eval_approx_result.json")
    req = scan_requests(task_dir / "requests.jsonl")

    eval_source, eval_status, resolved = pick_eval(official, approx)
    patch_status = patch_meta.get("patch_status") or run_meta.get("patch_status")
    modified_tests = bool(patch_meta.get("modified_test_files"))
    agent_status = run_meta.get("status")
    ftype = failure_type(agent_status, patch_status, eval_status, resolved, modified_tests)

    # eval detail (from whichever source won)
    ev = official if eval_source == "official" else (approx if eval_source == "approx" else {})
    f2p = (ev or {}).get("fail_to_pass") or {}
    f2p_pass = sum(1 for v in f2p.values() if v == "pass")
    collection_failed = any(v == "collection_failed" for v in f2p.values()) or \
        any(v == "collection_failed"
            for v in ((ev or {}).get("pass_to_pass", {}).get("results") or {}).values())

    return {
        "instance_id": iid,
        "agent_status": agent_status,
        "patch_status": patch_status,
        "modified_tests": modified_tests,
        "eval_source": eval_source,
        "eval_status": eval_status,
        "resolved": resolved,
        "failure_type": ftype,
        "fail_to_pass_passed": f2p_pass if f2p else None,
        "fail_to_pass_total": len(f2p) if f2p else None,
        "duration_seconds": run_meta.get("duration_seconds"),
        # tokens / cost / completeness
        **req,
        # graph
        "subagent_detected": graph.get("subagent_detected"),
        "subagent_reconstructed": graph.get("subagent_reconstructed"),
        "graph_confidence": graph.get("graph_confidence"),
        "n_threads": len(graph.get("threads", [])) or None,
        "orphan_nodes": len(graph.get("orphan_nodes", [])),
        "collection_failed": collection_failed,
        # presence flags
        "_has_model_patch": (task_dir / "model.patch").exists(),
        "_has_patch_meta": bool(patch_meta),
        "_has_session_graph": bool(graph),
        "_has_eval": eval_source != "none" or official is not None,
    }


def fmt_int(n) -> str:
    return f"{n:,}" if isinstance(n, int) else ("-" if n is None else str(n))


def render_main_table(rows: list[dict]) -> str:
    header = (
        "| instance_id | agent | patch | eval_src | eval_status | resolved | "
        "failure_type | subagent | recon | conf | mod_tests |\n"
        "|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|"
    )
    lines = [header]
    for r in rows:
        lines.append(
            f"| {r['instance_id']} | {r['agent_status']} | {r['patch_status']} | "
            f"{r['eval_source']} | {r['eval_status']} | {r['resolved']} | "
            f"{r['failure_type']} | {'Y' if r['subagent_detected'] else 'n'} | "
            f"{'Y' if r['subagent_reconstructed'] else 'n'} | {r['graph_confidence']} | "
            f"{'Y' if r['modified_tests'] else 'n'} |"
        )
    return "\n".join(lines)


def render_token_table(rows: list[dict]) -> str:
    header = (
        "| instance_id | calls | prompt_tok | compl_tok | total_tok | cost | F2P |\n"
        "|---|--:|--:|--:|--:|--:|:--:|"
    )
    lines = [header]
    tot = {"calls": 0, "p": 0, "c": 0, "t": 0, "cost": 0.0}
    npass = 0
    for r in rows:
        f2p = (f"{r['fail_to_pass_passed']}/{r['fail_to_pass_total']}"
               if r["fail_to_pass_total"] else "-")
        lines.append(
            f"| {r['instance_id']} | {r['calls']} | {fmt_int(r['prompt_tokens'])} | "
            f"{fmt_int(r['completion_tokens'])} | {fmt_int(r['total_tokens'])} | "
            f"${r['cost']:.2f} | {f2p} |")
        tot["calls"] += r["calls"]; tot["p"] += r["prompt_tokens"]
        tot["c"] += r["completion_tokens"]; tot["t"] += r["total_tokens"]
        tot["cost"] += r["cost"]; npass += int(r["resolved"] == "PASS")
    lines.append(
        f"| **TOTAL** | {tot['calls']} | {fmt_int(tot['p'])} | {fmt_int(tot['c'])} | "
        f"{fmt_int(tot['t'])} | ${tot['cost']:.2f} | {npass}/{len(rows)} PASS |")
    return "\n".join(lines)


def completeness_flags(rows: list[dict]) -> list[str]:
    problems = []
    for r in rows:
        iid = r["instance_id"]
        if r["calls"] == 0:
            problems.append(f"{iid}: no requests.jsonl records")
        if not r["_has_model_patch"]:
            problems.append(f"{iid}: missing_model_patch")
        if not r["_has_patch_meta"]:
            problems.append(f"{iid}: missing_patch_meta")
        if not r["_has_session_graph"]:
            problems.append(f"{iid}: missing_session_graph")
        if not r["_has_eval"]:
            problems.append(f"{iid}: missing_eval_result")
        if r["patch_status"] == "empty_patch":
            problems.append(f"{iid}: empty_patch (no fix)")
        if r["modified_tests"]:
            problems.append(f"{iid}: modified_tests (agent edited tests)")
        if r["orphan_nodes"]:
            problems.append(f"{iid}: {r['orphan_nodes']} orphan_nodes in graph")
        if r["collection_failed"]:
            problems.append(f"{iid}: collection_failed (a test did not collect)")
        if r["redaction_suspect_count"]:
            problems.append(f"{iid}: {r['redaction_suspect_count']} redaction_suspect!")
        if r["calls"] and r["missing_usage_records"]:
            problems.append(f"{iid}: {r['missing_usage_records']} records missing usage")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset")
    ap.add_argument("--out", default=str(LOGS_ROOT / "summary.json"))
    args = ap.parse_args()

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}

    if not LOGS_ROOT.exists():
        raise SystemExit(f"ERROR: {LOGS_ROOT} not found.")
    dirs = [p for p in sorted(LOGS_ROOT.iterdir())
            if p.is_dir() and ((p / "requests.jsonl").exists() or (p / "run_meta.json").exists())
            and (not ids_filter or p.name in ids_filter)]
    if not dirs:
        raise SystemExit("ERROR: no task logs found under logs/.")

    rows = [analyze_task(d) for d in dirs]

    print("\n## Status / taxonomy\n")
    print(render_main_table(rows))
    print("\n## Tokens / cost\n")
    print(render_token_table(rows))

    problems = completeness_flags(rows)
    if problems:
        print("\n## ⚠ Completeness / integrity flags\n")
        for p in problems:
            print(f"- {p}")
    else:
        print("\n(integrity: all artifacts present, usage complete, no redaction leaks)")

    summary = {
        "tasks": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        "totals": {
            "calls": sum(r["calls"] for r in rows),
            "total_tokens": sum(r["total_tokens"] for r in rows),
            "cost": round(sum(r["cost"] for r in rows), 4),
            "resolved_pass": sum(1 for r in rows if r["resolved"] == "PASS"),
            "n_tasks": len(rows),
            "by_failure_type": _count(rows, "failure_type"),
        },
        "completeness_problems": problems,
    }
    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[analyze] wrote {args.out}")


def _count(rows, key):
    out: dict = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


if __name__ == "__main__":
    main()
