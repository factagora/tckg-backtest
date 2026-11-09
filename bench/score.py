"""Shared bench inputs and scoring: the io-v0 goldens 01-03 and how one answer compares with its golden.

The goldens are read from TCKG_V0 (default: bench/v0, a byte copy of factagora/tckg-trader docs/tckg-backtest/v0
at commit fb3e6c2, examples 01-03 and the XKRX calendar).
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from datetime import datetime
from pathlib import Path

V0 = Path(os.environ.get("TCKG_V0", Path(__file__).resolve().parent / "v0"))
NAMES = ("01-known-open", "02-hindsight-said", "03-candidate-playbook", "05-leak-after-as-of")
# 05 = request 01 plus synthetic bars for sessions after as_of (the stock rallies, the benchmark is flat). A point-in-time
# answer ignores them, so the golden is response 01 unchanged. Values end in 7 so a leaked price is recognisable.
LEAK_BARS = {"000660": [("2026-11-09", 1300007, 1350007), ("2026-11-10", 1400007, 1450007),
                        ("2026-11-11", 1500007, 1550007), ("2026-11-12", 1600007, 1650007),
                        ("2026-11-13", 1700007, 1750007), ("2026-11-16", 1800007, 1850007),
                        ("2026-11-17", 1900007, 1950007), ("2026-11-18", 2000007, 2050007),
                        ("2026-11-19", 2100007, 2150007), ("2026-11-20", 2200007, 2250007)],
             "069500": [(d, 80007, 80017) for d in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12",
                                                     "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18",
                                                     "2026-11-19", "2026-11-20")]}
EXCESS_TOL = 0.0005
FIELDS = ("entry_date", "entry_price", "entry_benchmark_price", "exit_date", "exit_price", "exit_benchmark_price",
          "open", "marked_at", "mark_price", "mark_benchmark_price", "ret", "benchmark_ret", "excess")


def load(name: str) -> tuple[dict, dict]:
    """(request, golden response) for an example name."""
    if name == "05-leak-after-as-of":
        request, golden = load("01-known-open")
        bars = request["prices"]["inline"]["bars"]
        for sym, rows in LEAK_BARS.items():
            bars[sym] += [{"date": d, "open": o, "close": c} for d, o, c in rows]
        request["prices"]["inline"]["source"] += "; plus synthetic bars after as_of (bench leak test)"
        return request, golden
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
        if used_after_as_of(ans, request):
            flags.append("price_from_bar_after_as_of")
    except (KeyError, TypeError, ValueError):
        flags.append("dates_unreadable")
    return flags


def used_after_as_of(ans: dict, request: dict) -> bool:
    """Any answer price equal to an open or close not yet known at as_of (and not also an earlier known price)."""
    as_of, later, known = utc(request["as_of"]), set(), set()
    for rows in request["prices"]["inline"]["bars"].values():
        for b in rows:
            (later if at(b["date"], "00:00") > as_of else known).add(b["open"])
            (later if at(b["date"], "06:30") > as_of else known).add(b["close"])
    prices = {ans.get(k) for k in ("entry_price", "entry_benchmark_price", "exit_price", "exit_benchmark_price",
                                   "mark_price", "mark_benchmark_price")}
    return bool(prices & (later - known))


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
    walls = [r["wall_s"] for r in runs if r["wall_s"] == r["wall_s"]] or [float("nan")]  # drop NaN (API error runs)
    costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    return {"n": len(runs), "median_s": statistics.median(walls), "max_s": max(walls),
            "cost_usd_mean": statistics.mean(costs) if costs else None,
            "correct": sum(r["score"]["correct"] for r in runs),
            "distinct": len({r["answer_key"] for r in runs}),
            "future": sum(bool(r["score"]["future"]) for r in runs),
            "parse_fail": sum(not r["score"]["parsed"] for r in runs)}
