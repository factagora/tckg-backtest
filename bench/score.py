"""Shared bench inputs and scoring: the io-v0 goldens 01-03 and how one answer compares with its golden.

The goldens are read from TCKG_V0 (default: this repo's tckg_backtest/contract/v0, the byte copy of tckg-trader
docs/tckg-backtest/v0).
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V0 = Path(os.environ.get("TCKG_V0", REPO / "tckg_backtest" / "contract" / "v0"))
NAMES = ("01-known-open", "02-hindsight-said", "03-candidate-playbook")
EXCESS_TOL = 0.0005
FIELDS = ("entry_date", "entry_price", "entry_benchmark_price", "exit_date", "exit_price", "exit_benchmark_price",
          "open", "marked_at", "mark_price", "mark_benchmark_price", "ret", "benchmark_ret", "excess")


def load(name: str) -> tuple[dict, dict]:
    """(request, golden response) for an example name."""
    return (json.loads((V0 / f"request-{name}.json").read_text()),
            json.loads((V0 / f"response-{name}.json").read_text()))


def calendar() -> dict:
    return json.loads((V0 / "calendar-XKRX.json").read_text())


def answer_from_response(resp: dict) -> dict | None:
    """The first trade line of a v0 response in the flat answer shape the AI is asked for."""
    if not resp.get("trades"):
        return None
    t = resp["trades"][0]
    m = t.get("mark") or {}
    return {"entry_date": t["entry"]["date"], "entry_price": t["entry"]["price"],
            "entry_benchmark_price": t["entry"]["benchmark_price"], "exit_date": t["exit"]["date"],
            "exit_price": t["exit"]["price"], "exit_benchmark_price": t["exit"]["benchmark_price"], "open": t["open"],
            "marked_at": t["marked_at"], "mark_price": m.get("price"), "mark_benchmark_price": m.get("benchmark_price"),
            "ret": t["ret"], "benchmark_ret": t["benchmark_ret"], "excess": t["excess"]}


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def at(date: str, hhmm_utc: str) -> datetime:
    # shortcut: XKRX default session times (open 00:00Z, close 06:30Z); the 01-03 bars carry no open_at/close_at
    return utc(f"{date}T{hhmm_utc}:00Z")


def future_flags(ans: dict, request: dict, golden: dict) -> list[str]:
    """Ways the answer used something not known at as_of, or entered at or before the signal (golden trigger_at,
    which follows the request's mode)."""
    as_of, trigger = utc(request["as_of"]), utc(golden["trades"][0]["trigger_at"])
    flags = []
    try:
        if at(ans["entry_date"], "00:00") <= trigger:
            flags.append("entry_not_after_trigger")
        if at(ans["exit_date"], "00:00") > as_of and (ans.get("open") is not True or ans.get("exit_price") is not None):
            flags.append("exit_price_after_as_of")
        if ans.get("marked_at") and at(ans["marked_at"], "06:30") > as_of:
            flags.append("mark_after_as_of")
    except (KeyError, TypeError, ValueError):
        flags.append("dates_unreadable")
    return flags


def score(ans: dict | None, request: dict, golden: dict) -> dict:
    """parsed, dates_ok (entry and exit date exact), excess_ok (within EXCESS_TOL), correct (both), future flags."""
    if not isinstance(ans, dict):
        return {"parsed": False, "dates_ok": False, "excess_ok": False, "correct": False, "future": []}
    g = answer_from_response(golden)
    dates_ok = ans.get("entry_date") == g["entry_date"] and ans.get("exit_date") == g["exit_date"]
    ex = ans.get("excess")
    excess_ok = isinstance(ex, (int, float)) and not isinstance(ex, bool) and abs(ex - g["excess"]) <= EXCESS_TOL
    return {"parsed": True, "dates_ok": dates_ok, "excess_ok": excess_ok, "correct": dates_ok and excess_ok,
            "future": future_flags(ans, request, golden)}


def fingerprint(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def summarize(runs: list[dict]) -> dict:
    """runs: [{wall_s, cost_usd, answer_key, score}] for one request and one system."""
    return {"n": len(runs), "median_s": statistics.median(r["wall_s"] for r in runs),
            "max_s": max(r["wall_s"] for r in runs),
            "cost_usd_mean": statistics.mean(r["cost_usd"] for r in runs) if all(r["cost_usd"] is not None for r in runs) else None,
            "correct": sum(r["score"]["correct"] for r in runs),
            "distinct": len({r["answer_key"] for r in runs}),
            "future": sum(bool(r["score"]["future"]) for r in runs),
            "parse_fail": sum(not r["score"]["parsed"] for r in runs)}
