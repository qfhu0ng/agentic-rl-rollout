#!/usr/bin/env python3
"""Reconstruct the per-session request graph + RL-consumable rollouts.

requests.jsonl is a flat, time-ordered API log; the main agent and any spawned
sub-agents are INTERLEAVED in it. Codex (responses wire) is stateless — it
resends the full `input` history each turn and carries no previous_response_id —
so threads are separated by INPUT-PREFIX CONTAINMENT: within one thread, turn N+1's
input starts with turn N's input. When a record's input does NOT extend any open
thread's tip, it begins a new thread (a freshly-spawned sub-agent's private
conversation, which starts ~3 items fresh).

We are honest about confidence:
  subagent_detected      = a real spawn_agent/wait_agent call exists (reliable)
  subagent_reconstructed = we attributed >=1 non-main thread's nodes (approximate;
                           the sub-agent's LLM calls carry no parent UUID, so the
                           thread<->spawn link is by timing, not identity)

Outputs per task:
  logs/<id>/session_graph.json   nodes / edges / threads / orphan_nodes
  logs/<id>/rollouts.jsonl       one row per thread: ordered steps
                                 {seq, new_input_items (delta), output, tool_calls, usage}

Usage:
    python3 build_rollouts.py
    python3 build_rollouts.py --ids sympy__sympy-21171 --max-item-chars 4000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LOGS_ROOT = REPO_ROOT / "logs"

SUBAGENT_NAMES = ("multi_agent_v1", "spawn_agent", "wait_agent", "close_agent")
SPAWN_NAMES = ("spawn_agent", "multi_agent_v1")
JOIN_NAMES = ("wait_agent", "close_agent")


# --------------------------------------------------------------------------- #
# Record helpers (tolerant of old logs without derived fields)
# --------------------------------------------------------------------------- #

def _input_items(rec: dict) -> list:
    kw = rec.get("kwargs") or {}
    items = kw.get("input")
    return items if isinstance(items, list) else []


def _item_key(it) -> str:
    if not isinstance(it, dict):
        return "h:" + hashlib.md5(json.dumps(it, default=str).encode()).hexdigest()[:12]
    if it.get("id"):
        return "id:" + str(it["id"])
    if it.get("call_id"):
        return "cid:" + str(it["call_id"]) + ":" + str(it.get("type", ""))
    return "h:" + hashlib.md5(
        json.dumps(it, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _tool_calls(rec: dict) -> list[dict]:
    # Prefer the derived field (new logs); fall back to scanning response output.
    fcs = rec.get("function_calls")
    if isinstance(fcs, list) and fcs:
        return fcs
    out = []
    resp = rec.get("response_obj") or {}
    for it in (resp.get("output") or []):
        if isinstance(it, dict) and it.get("type") in ("function_call", "custom_tool_call", "tool_call"):
            name = it.get("name")
            out.append({"name": name, "call_id": it.get("call_id"),
                        "is_subagent": bool(name and any(k in str(name) for k in SUBAGENT_NAMES))})
    return out


def _spawn_agent_type(rec: dict) -> str | None:
    """Pull agent_type from a spawn_agent call's arguments, if present."""
    resp = rec.get("response_obj") or {}
    for it in (resp.get("output") or []):
        if isinstance(it, dict) and it.get("name") in SPAWN_NAMES:
            args = it.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001
                    args = {}
            if isinstance(args, dict) and args.get("agent_type"):
                return str(args["agent_type"])
    return None


def _usage_total(rec: dict) -> dict:
    u = rec.get("usage") or {}
    if not isinstance(u, dict):
        return {}
    return {"prompt_tokens": u.get("prompt_tokens"),
            "completion_tokens": u.get("completion_tokens"),
            "total_tokens": u.get("total_tokens")}


def _truncate(obj, max_chars: int):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return obj
    return {"_truncated": True, "_orig_chars": len(s), "preview": s[:max_chars]}


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #

def load_records(req_path: Path) -> list[dict]:
    recs = []
    with open(req_path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                continue
    # Order: prefer seq (new logs), else timestamp, else file order.
    has_seq = all("seq" in r for r in recs)
    if has_seq:
        recs.sort(key=lambda r: r["seq"])
    else:
        recs.sort(key=lambda r: (r.get("timestamp_start") or "", r.get("request_id") or ""))
    return recs, has_seq


def assign_threads(recs: list[dict]) -> tuple[list[dict], list[str], dict]:
    """Greedy prefix-containment threading.

    Returns (threads, attach_kinds, stats) where threads is a list of
    {tid, parent, agent_type, indices:[rec_idx...]} and attach_kinds[i] is one of
    'continues' / 'root' / 'temporal_fallback'.
    """
    keys_per_rec = [[_item_key(it) for it in _input_items(r)] for r in recs]
    threads: list[dict] = []
    attach_kind: list[str] = [""] * len(recs)
    rec_thread: list[int] = [-1] * len(recs)

    for i, keys in enumerate(keys_per_rec):
        # Degenerate record (empty input — e.g. a failed/aborted call): never let
        # it anchor or extend a thread (an empty tip would swallow everything).
        if not keys:
            attach_kind[i] = "orphan"
            rec_thread[i] = -1
            continue
        best_t, best_len = -1, -1
        for ti, T in enumerate(threads):
            L = T["last_keys"]
            if len(keys) > len(L) and keys[:len(L)] == L and len(L) > best_len:
                best_t, best_len = ti, len(L)
        if best_t >= 0 and best_len >= 1:
            T = threads[best_t]
            attach_kind[i] = "continues"
            rec_thread[i] = best_t
            T["indices"].append(i)
            T["last_keys"] = keys
        else:
            # New thread: the first substantive call (main) or a freshly-spawned
            # sub-agent's private conversation (starts ~3 items fresh).
            tid = len(threads)
            threads.append({"indices": [i], "last_keys": keys})
            rec_thread[i] = tid
            attach_kind[i] = "root"

    stats = {
        "n_threads": len(threads),
        "n_continues": attach_kind.count("continues"),
        "n_roots": attach_kind.count("root"),
        "n_orphan": attach_kind.count("orphan"),
        "n_temporal_fallback": attach_kind.count("temporal_fallback"),
    }
    return threads, attach_kind, rec_thread, stats


def node_id(rec: dict, idx: int) -> str:
    seq = rec.get("seq")
    return f"req_{seq:06d}" if isinstance(seq, int) else f"req_idx_{idx:06d}"


def build_graph(recs: list[dict], has_seq: bool, max_item_chars: int) -> tuple[dict, list[dict]]:
    threads, attach_kind, rec_thread, stats = assign_threads(recs)

    # Main thread = the earliest-starting real thread (orphan/degenerate records
    # carry no thread, so they can't steal "main").
    main_tid = 0
    if threads:
        main_tid = min(range(len(threads)), key=lambda ti: threads[ti]["indices"][0])
    # Name threads.
    tid_name = {}
    for ti in range(len(threads)):
        tid_name[ti] = "main" if ti == main_tid else f"thread_{ti}"
    tid_name[-1] = "orphan"

    # Nodes.
    nodes = []
    node_of_idx = {}
    for idx, rec in enumerate(recs):
        nid = node_id(rec, idx)
        node_of_idx[idx] = nid
        nodes.append({
            "node_id": nid,
            "seq": rec.get("seq", idx),
            "request_id": rec.get("request_id"),
            "response_id": rec.get("response_id"),
            "thread_id": tid_name[rec_thread[idx]],
            "status": rec.get("status"),
            "usage": _usage_total(rec),
            "tool_calls": _tool_calls(rec),
            "attach": attach_kind[idx],
        })

    # Edges: continues (within thread, consecutive), spawns/joins, temporal fallback.
    edges = []
    for T in threads:
        inds = T["indices"]
        for a, b in zip(inds, inds[1:]):
            kind = attach_kind[b] if attach_kind[b] == "temporal_fallback" else "continues"
            edges.append({"from": node_of_idx[a], "to": node_of_idx[b], "type": kind})

    # spawns: a node with a spawn call -> first node of the next new (non-main)
    # thread that starts after it (by order). joins: a wait/close node links the
    # sub-thread's last node back to it.
    # Order threads by their first record index for time-proximity linking.
    sub_threads = sorted(
        [ti for ti in range(len(threads)) if ti != main_tid],
        key=lambda ti: threads[ti]["indices"][0])
    spawn_nodes = [(idx, rec) for idx, rec in enumerate(recs)
                   if any(tc.get("name") in SPAWN_NAMES for tc in _tool_calls(rec))]
    used_sub = set()
    for spawn_idx, _ in spawn_nodes:
        # nearest following sub-thread not yet linked
        cand = None
        for ti in sub_threads:
            if ti in used_sub:
                continue
            if threads[ti]["indices"][0] > spawn_idx:
                cand = ti
                break
        if cand is not None:
            used_sub.add(cand)
            edges.append({"from": node_of_idx[spawn_idx],
                          "to": node_of_idx[threads[cand]["indices"][0]],
                          "type": "spawns"})
            # agent_type from the spawn arguments
            at = _spawn_agent_type(recs[spawn_idx])
            if at:
                tid_name_idx = cand
                threads[cand]["agent_type"] = at
    # joins: link each sub-thread's last node to the next wait/close node in main.
    join_nodes = [idx for idx, rec in enumerate(recs)
                  if any(tc.get("name") in JOIN_NAMES for tc in _tool_calls(rec))]
    for ti in sub_threads:
        last_idx = threads[ti]["indices"][-1]
        follow = [j for j in join_nodes if j >= last_idx]
        if follow:
            edges.append({"from": node_of_idx[last_idx],
                          "to": node_of_idx[follow[0]], "type": "joins"})

    # Thread records.
    thread_recs = []
    for ti in range(len(threads)):
        thread_recs.append({
            "thread_id": tid_name[ti],
            "parent_thread_id": None if ti == main_tid else "main",
            "agent_type": "main" if ti == main_tid else threads[ti].get("agent_type", "unknown"),
            "node_count": len(threads[ti]["indices"]),
        })

    subagent_detected = any(
        tc.get("is_subagent") for n in nodes for tc in n["tool_calls"])
    subagent_reconstructed = len(threads) > 1

    # Confidence.
    if not has_seq:
        confidence = "low" if stats["n_temporal_fallback"] else "medium"
    else:
        frac_fallback = stats["n_temporal_fallback"] / max(1, len(recs))
        if frac_fallback == 0:
            confidence = "high"
        elif frac_fallback < 0.1:
            confidence = "medium"
        else:
            confidence = "low"

    # Orphan/degenerate records: list them and link temporally to the prior node.
    orphan_nodes = [node_of_idx[i] for i in range(len(recs))
                    if attach_kind[i] in ("temporal_fallback", "orphan")]
    for i in range(1, len(recs)):
        if attach_kind[i] == "orphan":
            edges.append({"from": node_of_idx[i - 1], "to": node_of_idx[i],
                          "type": "temporal_fallback"})

    graph = {
        "task_id": recs[0].get("task_id") if recs else None,
        "session_id": recs[0].get("session_id") if recs else None,
        "graph_confidence": confidence,
        "linear": len(threads) == 1,
        "subagent_detected": subagent_detected,
        "subagent_reconstructed": subagent_reconstructed,
        "n_records": len(recs),
        "stats": stats,
        "threads": thread_recs,
        "nodes": nodes,
        "edges": edges,
        "orphan_nodes": orphan_nodes,
    }

    # Rollouts: per thread, ordered steps with input DELTA (new items this turn).
    rollouts = []
    for ti in range(len(threads)):
        inds = threads[ti]["indices"]
        steps = []
        prev_len = 0
        for idx in inds:
            rec = recs[idx]
            items = _input_items(rec)
            delta = items[prev_len:] if len(items) >= prev_len else items
            prev_len = len(items)
            resp = rec.get("response_obj") or {}
            steps.append({
                "seq": rec.get("seq", idx),
                "node_id": node_of_idx[idx],
                "status": rec.get("status"),
                "new_input_items": _truncate(delta, max_item_chars),
                "output": _truncate(resp.get("output") or [], max_item_chars),
                "tool_calls": _tool_calls(rec),
                "usage": _usage_total(rec),
            })
        rollouts.append({
            "thread_id": tid_name[ti],
            "parent_thread_id": None if ti == main_tid else "main",
            "agent_type": "main" if ti == main_tid else threads[ti].get("agent_type", "unknown"),
            "steps": steps,
        })

    return graph, rollouts


def process_task(task_dir: Path, max_item_chars: int) -> dict | None:
    req_path = task_dir / "requests.jsonl"
    if not req_path.exists():
        return None
    recs, has_seq = load_records(req_path)
    if not recs:
        return None
    graph, rollouts = build_graph(recs, has_seq, max_item_chars)
    (task_dir / "session_graph.json").write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(task_dir / "rollouts.jsonl", "w", encoding="utf-8") as f:
        for r in rollouts:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return graph


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset")
    ap.add_argument("--max-item-chars", type=int, default=4000,
                    help="truncate any single input/output item larger than this in rollouts.jsonl")
    args = ap.parse_args()

    ids_filter = None
    if args.ids:
        ids_filter = {s.strip() for s in args.ids.split(",") if s.strip()}

    if not LOGS_ROOT.exists():
        sys.exit(f"ERROR: {LOGS_ROOT} not found.")

    dirs = [p for p in sorted(LOGS_ROOT.iterdir())
            if p.is_dir() and (p / "requests.jsonl").exists()
            and (not ids_filter or p.name in ids_filter)]
    if not dirs:
        sys.exit("ERROR: no task logs with requests.jsonl found.")

    for d in dirs:
        g = process_task(d, args.max_item_chars)
        if g is None:
            print(f"[build_rollouts] {d.name}: no records, skipped")
            continue
        print(f"[build_rollouts] {d.name}: {g['n_records']} records, "
              f"{len(g['threads'])} thread(s), "
              f"subagent_detected={g['subagent_detected']} "
              f"reconstructed={g['subagent_reconstructed']} "
              f"confidence={g['graph_confidence']}")
    print(f"[build_rollouts] done for {len(dirs)} task(s).")


if __name__ == "__main__":
    main()
