"""Time tckg-backtest run_backtest on the same requests the AI baseline gets.

    python3 bench/engine.py --runs 5

Uses tckg_backtest.run_backtest from this checkout. If it is not importable (engine not merged yet), times a
stand-in that returns the golden response, and records kind "mock". Writes bench/results/<UTC timestamp>-engine.json.
"""
from __future__ import annotations

import argparse
import copy
import inspect
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from score import NAMES, answer_from_response, fingerprint, load, score, summarize  # noqa: E402

GOLDENS = {n: load(n) for n in NAMES}


def engine():
    """(callable(request) -> response, label)."""
    try:
        from tckg_backtest import run_backtest
    except ImportError as e:
        # shortcut: the stand-in only serves the contract's own requests (01-03); 05 is skipped until the engine lands
        by_req = {json.dumps(load(n)[0], sort_keys=True): load(n)[1] for n in NAMES if not n.startswith("05")}
        return (lambda req: copy.deepcopy(by_req[json.dumps(req, sort_keys=True)])), f"mock ({e})"
    params = inspect.signature(run_backtest).parameters
    kw = {}
    if "load_prices" in params:
        kw["load_prices"] = None  # 01-03 carry inline bars
    if "now" in params:
        kw["now"] = datetime(2026, 11, 8, tzinfo=UTC)  # golden computed_at
    return (lambda req: run_backtest(req, **kw)), "tckg_backtest.run_backtest"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    a = ap.parse_args(argv)
    run, label = engine()
    commit = subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    out = {"kind": "engine", "engine": label, "commit": commit, "python": sys.version.split()[0],
           "started_at": datetime.now(UTC).isoformat(timespec="seconds"), "requests": {}}
    for name, (request, golden) in GOLDENS.items():
        if label.startswith("mock") and name.startswith("05"):
            out["requests"][name] = {"summary": None, "skipped": "mock engine has no answer for a non-contract request"}
            continue
        runs = []
        for i in range(a.runs):
            req = copy.deepcopy(request)
            t0 = time.perf_counter()
            resp = run(req)
            wall = time.perf_counter() - t0
            ans = answer_from_response(resp)
            cert = resp.get("certificate") or {}
            runs.append({"run": i + 1, "wall_s": wall, "cost_usd": 0.0, "answer": ans,
                         "answer_key": fingerprint(ans), "score": score(ans, request, golden),
                         "certificate_mock": cert.get("mock"), "engine_version": cert.get("engine_version")})
            print(f"{name} run {i + 1}: {wall * 1e3:.3f} ms {runs[-1]['score']}", flush=True)
        out["requests"][name] = {"summary": summarize(runs), "runs": runs}
    out["mock"] = label.startswith("mock") or any(
        r["certificate_mock"] for q in out["requests"].values() for r in q["runs"])
    path = HERE / "results" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-engine.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
