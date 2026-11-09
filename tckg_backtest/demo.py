"""No-token demo for the console page: runs only the bundled contract requests (and the bench 05 leak variant)
through the real engine, with an optional as_of override. Clients cannot send prices or playbooks."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import time
from datetime import date, timedelta
from functools import cache
from pathlib import Path

from tckg_backtest.core import CONTRACT_DIR
from tckg_backtest.claims import run_claims_backtest
from tckg_backtest.engine import run_backtest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
AI_RESULT = BENCH / "results" / "20261109T013857Z-ai-claude-sonnet-5-5-short.json"
CASES = {
    "01": ("01-known-open", "Follow from when it was known"),
    "02": ("02-hindsight-said", "Hindsight: from the day it was said"),
    "03": ("03-candidate-playbook", "Candidate Playbook"),
    "05": ("05-leak-after-as-of", "Leak trap: prices after as_of"),
    "06": ("06-many-claims", "Many claims (mock data)"),
}
# 06 runs on contract v1 (many FactBlocks, claims Playbook) with mock FactBlocks and synthetic prices.
MOCK_06 = Path(__file__).resolve().parent / "demo-06-many-claims.json"


def _json(path: Path):
    return json.loads(path.read_text())


@cache
def _leak_bars() -> dict:
    spec = importlib.util.spec_from_file_location("bench_score", BENCH / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.LEAK_BARS


def request(case: str) -> dict:
    """The bundled request for a case (a fresh copy). 05 = contract request 01 plus bench/score.py LEAK_BARS."""
    name = CASES[case][0]
    if case == "06":
        return _json(MOCK_06)
    if case != "05":
        return _json(CONTRACT_DIR / f"request-{name}.json")
    req = _json(CONTRACT_DIR / "request-01-known-open.json")
    for sym, rows in _leak_bars().items():
        req["prices"]["inline"]["bars"][sym] += [{"date": d, "open": o, "close": c} for d, o, c in rows]
    req["prices"]["inline"]["source"] += "; plus synthetic bars after as_of (bench leak test)"
    return req


def response_sha256(resp: dict) -> str:
    """Hash of the response without computed_at (the only field that follows the clock)."""
    body = {k: v for k, v in resp.items() if k != "computed_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run(case: str, as_of: str | None) -> dict:
    req = request(case)
    if as_of:
        req["as_of"] = as_of
    t0 = time.perf_counter()
    resp = run_claims_backtest(req) if case == "06" else run_backtest(req)
    ms = (time.perf_counter() - t0) * 1e3
    return {"case": case, "as_of": req["as_of"], "server_ms": ms, "response_sha256": response_sha256(resp),
            "response": resp}


def _sessions() -> list[str]:
    cal = _json(CONTRACT_DIR / "calendar-XKRX.json")
    hol, d, out = set(cal["holidays"]), date(2026, 8, 3), []
    while d <= date(2026, 11, 30):
        if d.weekday() < 5 and d.isoformat() not in hol:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _ai(name: str) -> dict | None:
    if not AI_RESULT.exists():
        return None
    d = _json(AI_RESULT)
    q = d["requests"].get(name)
    if not q:
        return None
    return {"model": d["model"], "prompt_kind": d["prompt_kind"], "file": f"bench/results/{AI_RESULT.name}",
            "instruction": d.get("instruction"),
            "runs": [{"run": r["run"], "wall_s": r["wall_s"], "answer": r["answer"], "correct": r["score"]["correct"],
                      "future": r["score"].get("future", [])} for r in q["runs"]]}


@cache
def info() -> dict:
    """Everything the page shows that does not need the engine: cases, factblocks, playbooks, sessions, AI runs."""
    cases = {}
    for case, (name, label) in CASES.items():
        req = request(case)
        if case == "06":
            cases[case] = {"label": label, "name": name, "contract": "v1", "as_of": req["as_of"], "mode": req["mode"],
                           "factblocks": req["factblocks"], "playbook": req["playbook"],
                           "prices_source": req["prices"]["inline"]["source"], "bars": req["prices"]["inline"]["bars"],
                           "sessions": [b["date"] for b in req["prices"]["inline"]["bars"]["ACME"]], "ai": None}
            continue
        cases[case] = {"label": label, "name": name, "as_of": req["as_of"], "mode": req["mode"],
                       "factblock": req["factblock"], "playbook": req["playbook"],
                       "prices_source": req["prices"]["inline"]["source"], "bars": req["prices"]["inline"]["bars"],
                       "ai": _ai(name)}
    return {"cases": cases, "sessions": _sessions()}
