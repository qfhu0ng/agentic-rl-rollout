#!/usr/bin/env python3
"""In-container approximate SWE-bench grader (mounted read-only, run per task).

Runs INSIDE the grade container. Builds a PRISTINE base from the mounted
workspace's git history (NOT cp-a-and-run, which drags in stray files), installs
deps ONCE as an editable install (so source edits are picked up live), then runs
two phases:

  baseline : apply test.patch only, run FAIL_TO_PASS  -> they MUST fail
             (if any passes at base, the test doesn't exercise the fix ->
              baseline_invalid; resolved=unknown)
  eval     : apply model.patch (product code) + test.patch, run F2P + P2P

Precise test selection: `pytest --collect-only` resolves bare names (sympy uses
`test_latex_basic`, not a nodeid) to exact nodeids, killing the `-k` substring
bug. pytest exit 5 (nothing collected) => collection_failed, not "fail".

Inputs via env:
  TASK_ID, BASE_COMMIT, INSTALL_CMD,
  FAIL_TO_PASS (json), PASS_TO_PASS (json, possibly sampled),
  PASS_TO_PASS_SAMPLED ("1"/"0"), PASS_TO_PASS_LIMIT, TEST_FILES (json),
  PER_TEST_TIMEOUT (seconds, default 300)

Mounts:
  /workspace          agent result (read-only; used only for its .git history)
  /eval/test.patch    eval patch (read-only)
  /logs/<TASK_ID>/    output dir (model.patch read from here too)

Writes /logs/<TASK_ID>/eval_approx_result.json with the full failure taxonomy.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

GRADE_WS = Path("/tmp/grade_ws")
TEST_PATCH = Path("/eval/test.patch")


def sh(cmd: str, cwd: Path | None = None, log: Path | None = None,
       timeout: int | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") + f"\n[grade] TIMEOUT after {timeout}s\n"
        rc = -9
    if log:
        log.write_text(out, encoding="utf-8")
    return rc, out


def collect_nodeids(test_files: list[str], cwd: Path) -> list[str]:
    """Return all nodeids pytest can collect from the given test files."""
    files = " ".join(f'"{f}"' for f in test_files)
    rc, out = sh(f"python3 -m pytest --collect-only -q -o addopts='' {files}", cwd=cwd)
    nodeids = []
    for ln in out.splitlines():
        ln = ln.strip()
        # pytest -q --collect-only prints one nodeid per line (file::...::test).
        if "::" in ln and not ln.startswith(("=", "<", "warning", "ERROR", "no tests")):
            nodeids.append(ln)
    return nodeids


def resolve_nodeid(name: str, collected: list[str]) -> list[str]:
    """Map a FAIL_TO_PASS / PASS_TO_PASS entry to concrete nodeid(s).

    If it already contains '::' it's a nodeid (match by prefix, ignoring params).
    Otherwise it's a bare function name (sympy style) -> exact last-segment match.
    """
    if "::" in name:
        # Exact, or matches parametrized variants name[...]
        exact = [n for n in collected if n == name]
        if exact:
            return exact
        return [n for n in collected if n.split("[")[0] == name.split("[")[0]]
    # Bare function name: match the final ::segment exactly (strip params).
    out = []
    for n in collected:
        last = n.split("::")[-1].split("[")[0]
        if last == name:
            out.append(n)
    return out


def run_nodeids(nodeids: list[str], cwd: Path, log: Path,
                timeout: int) -> bool:
    """Run a set of nodeids together; True iff pytest exits 0 (all passed)."""
    args = " ".join(f'"{n}"' for n in nodeids)
    rc, _ = sh(f"python3 -m pytest -p no:cacheprovider -q --no-header -o addopts='' {args}",
               cwd=cwd, log=log, timeout=timeout)
    return rc == 0


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]


def main() -> None:
    task_id = os.environ["TASK_ID"]
    base_commit = os.environ["BASE_COMMIT"]
    install_cmd = os.environ.get("INSTALL_CMD", "").strip() or "pip install -e ."
    fail_to_pass = json.loads(os.environ.get("FAIL_TO_PASS", "[]"))
    pass_to_pass = json.loads(os.environ.get("PASS_TO_PASS", "[]"))
    p2p_sampled = os.environ.get("PASS_TO_PASS_SAMPLED", "0") == "1"
    p2p_limit = os.environ.get("PASS_TO_PASS_LIMIT", "all")
    test_files = json.loads(os.environ.get("TEST_FILES", "[]"))
    per_test_timeout = int(os.environ.get("PER_TEST_TIMEOUT", "300"))

    log_dir = Path("/logs") / task_id
    tests_log_dir = log_dir / "eval_tests"
    tests_log_dir.mkdir(parents=True, exist_ok=True)
    model_patch = log_dir / "model.patch"

    result = {
        "task_id": task_id,
        "eval_source": "approx",
        "patch_status": None,          # set to invalid_patch if model.patch won't apply
        "test_patch_apply_ok": False,
        "install_ok": False,
        "eval_status": "ok",           # taxonomy: ok/install_failed/test_patch_failed/
                                       #           collection_failed/baseline_invalid/
                                       #           suspect_env/timeout
        "resolved": "unknown",
        "baseline": {},                # F2P results WITHOUT the fix (should be fail)
        "fail_to_pass": {},
        "pass_to_pass": {"summary": {"passed": 0, "total": 0},
                         "sampled": p2p_sampled, "limit": p2p_limit, "results": {}},
        "test_files": test_files,
        "notes": [],
    }

    def finish(note: str | None = None) -> None:
        if note:
            result["notes"].append(note)
        (log_dir / "eval_approx_result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        print(f"[grade:{task_id}] resolved={result['resolved']} "
              f"eval_status={result['eval_status']} "
              f"apply_ok={result['test_patch_apply_ok']} install_ok={result['install_ok']}")

    def apply_test_patch() -> bool:
        for tf in test_files:
            sh(f'git checkout {base_commit} -- "{tf}"', cwd=GRADE_WS)
        rc, _ = sh(f"git apply --verbose {TEST_PATCH}", cwd=GRADE_WS,
                   log=log_dir / "eval_apply.log")
        if rc != 0:
            rc2, _ = sh(f"git apply --3way {TEST_PATCH}", cwd=GRADE_WS)
            return rc2 == 0
        return True

    def restore_tests() -> None:
        for tf in test_files:
            sh(f'git checkout {base_commit} -- "{tf}"', cwd=GRADE_WS)

    # 1. Pristine base from the workspace's git history (drop all agent changes).
    rc, out = sh(f"cp -a /workspace {GRADE_WS}")
    if rc != 0:
        result["eval_status"] = "harness_failed"
        finish(f"cp -a failed: {out[:400]}")
        sys.exit(0)
    sh(f"git reset --hard {base_commit}", cwd=GRADE_WS)
    sh("git clean -fdx", cwd=GRADE_WS)

    # 2. Install deps ONCE (editable: later source edits are picked up live).
    rc, _ = sh(install_cmd, cwd=GRADE_WS, log=log_dir / "eval_install_stdout.log",
               timeout=1200)
    rc_pt, _ = sh("python3 -m pytest --version", cwd=GRADE_WS)
    if rc_pt != 0:
        sh("pip install pytest", cwd=GRADE_WS, log=log_dir / "eval_install_pytest.log")
        rc_pt, _ = sh("python3 -m pytest --version", cwd=GRADE_WS)
    # Verify the package itself imports (editable installs can rc=0 yet be broken).
    result["install_ok"] = (rc == 0) and (rc_pt == 0)
    if not result["install_ok"]:
        result["eval_status"] = "install_failed"
        result["resolved"] = "unknown"
        finish(f"install_cmd rc={rc}, pytest available={rc_pt == 0}")
        sys.exit(0)

    # 3. BASELINE: test.patch only (no fix). F2P should FAIL here.
    if not apply_test_patch():
        result["eval_status"] = "test_patch_failed"
        result["resolved"] = "unknown"
        finish("test.patch did not apply onto base (env mismatch)")
        sys.exit(0)
    result["test_patch_apply_ok"] = True

    collected = collect_nodeids(test_files, GRADE_WS)
    if not collected:
        result["eval_status"] = "collection_failed"
        result["resolved"] = "unknown"
        finish("pytest collected 0 tests from test_files after test.patch")
        sys.exit(0)

    baseline_any_pass = False
    for name in fail_to_pass:
        nids = resolve_nodeid(name, collected)
        if not nids:
            result["baseline"][name] = "collection_failed"
            continue
        passed = run_nodeids(nids, GRADE_WS, tests_log_dir / f"baseline_{sanitize(name)}.log",
                             per_test_timeout)
        result["baseline"][name] = "pass" if passed else "fail"
        baseline_any_pass = baseline_any_pass or passed

    if baseline_any_pass:
        result["eval_status"] = "baseline_invalid"
        result["resolved"] = "unknown"
        finish("a FAIL_TO_PASS test already passes at base (does not exercise the fix)")
        sys.exit(0)

    # 4. EVAL: apply model.patch (product fix) + test.patch, run F2P + P2P.
    restore_tests()
    if model_patch.exists() and model_patch.stat().st_size > 0:
        rc, _ = sh(f"git apply --verbose {model_patch}", cwd=GRADE_WS,
                   log=log_dir / "eval_model_apply.log")
        if rc != 0:
            rc2, _ = sh(f"git apply --3way {model_patch}", cwd=GRADE_WS)
            if rc2 != 0:
                result["patch_status"] = "invalid_patch"
                result["resolved"] = "unknown"
                finish("model.patch did not apply onto base")
                sys.exit(0)
    else:
        # No fix to apply: F2P will fail; record and let analyze tag no_patch.
        result["notes"].append("model.patch empty/missing")

    if not apply_test_patch():
        result["eval_status"] = "test_patch_failed"
        result["resolved"] = "unknown"
        finish("test.patch did not apply after model.patch")
        sys.exit(0)
    collected = collect_nodeids(test_files, GRADE_WS)

    f2p_all_pass = True
    for name in fail_to_pass:
        nids = resolve_nodeid(name, collected)
        if not nids:
            result["fail_to_pass"][name] = "collection_failed"
            f2p_all_pass = False
            continue
        passed = run_nodeids(nids, GRADE_WS, tests_log_dir / f"f2p_{sanitize(name)}.log",
                             per_test_timeout)
        result["fail_to_pass"][name] = "pass" if passed else "fail"
        f2p_all_pass = f2p_all_pass and passed

    p2p_all_pass = True
    p2p_passed = 0
    for name in pass_to_pass:
        nids = resolve_nodeid(name, collected)
        if not nids:
            result["pass_to_pass"]["results"][name] = "collection_failed"
            p2p_all_pass = False
            continue
        passed = run_nodeids(nids, GRADE_WS, tests_log_dir / f"p2p_{sanitize(name)}.log",
                             per_test_timeout)
        result["pass_to_pass"]["results"][name] = "pass" if passed else "fail"
        p2p_passed += int(passed)
        p2p_all_pass = p2p_all_pass and passed
    result["pass_to_pass"]["summary"] = {"passed": p2p_passed, "total": len(pass_to_pass)}

    # 5. Verdict. P2P all-fail (0/N) is an env-broken signal, not a real FAIL.
    if pass_to_pass and p2p_passed == 0:
        result["eval_status"] = "suspect_env"
        result["resolved"] = "unknown"
        finish("PASS_TO_PASS 0/N passed -> suspect broken eval environment")
        sys.exit(0)

    result["resolved"] = "PASS" if (f2p_all_pass and p2p_all_pass) else "FAIL"
    finish()
    sys.exit(0)


if __name__ == "__main__":
    main()
