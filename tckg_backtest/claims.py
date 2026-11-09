"""Contract v1: many FactBlocks, each with an investable call, x one claims Playbook -> trades, a unit-bet portfolio
and a certificate. The rules follow factagora.ai claims_v1 (lib/backtest/belief-backtest.ts, rules v1/v1.1) so the
same inputs give the same trades in hindsight_said_at mode; as_known mode triggers on known_at instead of said_at.

Pure: no network, no clock apart from `now` (as_of_in_future only). contract/v1/README.md has the rules in prose.
"""
from __future__ import annotations

import bisect
import json
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema

from tckg_backtest.canonical import canonical, request_sha256, sha256
from tckg_backtest.core import ENGINE_VERSION, _iso

CONTRACT_V1 = "tckg-backtest-io/v1"
V1_DIR = Path(__file__).resolve().parent.parent / "contract" / "v1"
NY = ZoneInfo("America/New_York")
US_CLOSE = time(16, 0)
ASSUMPTIONS = ["one equal unit per call, no compounding, positions may overlap",
               "entered at the open of the first bar after the trigger's exchange date",
               "exited at the close of the first bar on or after entry date + horizon days",
               "the bars given are the session calendar; prices as given (source in prices.inline.source)",
               "no dividends, no taxes; cost_bps charged on entry and on exit"]


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@cache
def _validator():
    return jsonschema.Draft202012Validator(json.loads((V1_DIR / "request.schema.json").read_text()))


def validate_request_v1(request) -> list[dict]:
    if not isinstance(request, dict):
        return [{"code": "invalid_request", "message": "The request body must be a JSON object."}]
    if "contract" in request and request["contract"] != CONTRACT_V1:
        return [{"code": "contract_mismatch", "field": "contract",
                 "message": f"contract must be {CONTRACT_V1!r}, got {request['contract']!r}."}]
    errors = [{"code": "invalid_request", "message": e.message,
               **({"field": ".".join(map(str, e.absolute_path))} if e.absolute_path else {})}
              for e in sorted(_validator().iter_errors(request), key=lambda e: list(map(str, e.absolute_path)))]
    if errors:
        return errors
    try:
        canonical(request)
    except (ValueError, TypeError) as e:
        return [{"code": "invalid_request", "message": str(e)}]
    for sym, bars in request["prices"]["inline"]["bars"].items():
        ds = [b["date"] for b in bars]
        if any(a >= b for a, b in zip(ds, ds[1:])):
            errors.append({"code": "invalid_request", "field": f"prices.inline.bars.{sym}",
                           "message": "Bars must be in strictly increasing date order."})
    ids = [f["id"] for f in request["factblocks"]]
    if len(set(ids)) != len(ids):
        errors.append({"code": "invalid_request", "field": "factblocks", "message": "FactBlock ids must be unique."})
    return errors


def _response(status, rsha, now, *, trades=(), skipped=(), summary=None, certificate=None, errors=()):
    return {"contract": CONTRACT_V1, "status": status, "request_sha256": rsha, "trades": list(trades),
            "skipped": list(skipped), "summary": summary, "certificate": certificate, "errors": list(errors),
            "computed_at": _iso(now)}


def _local_date(iso: str, market: str | None) -> str:
    """The exchange's calendar date of an instant (claims_v1 localDate): UTC for crypto, New York otherwise."""
    t = _t(iso)
    return (t.astimezone(UTC) if market == "CRYPTO" else t.astimezone(NY)).date().isoformat()


def _close_at(d: str, market: str | None) -> datetime:
    """When a daily bar's close is known: 16:00 New York for US, the end of the UTC day for crypto."""
    day = date.fromisoformat(d)
    if market == "CRYPTO":
        return datetime.combine(day, time(23, 59, 59), UTC)
    return datetime.combine(day, US_CLOSE, NY).astimezone(UTC)


def _open_at(d: str, market: str | None) -> datetime:
    """When a daily bar opens: 09:30 New York for US, the start of the UTC day for crypto."""
    day = date.fromisoformat(d)
    if market == "CRYPTO":
        return datetime.combine(day, time(0, 0), UTC)
    return datetime.combine(day, time(9, 30), NY).astimezone(UTC)


class _Series:
    """A symbol's bars known at as_of (close known by then); dates is the bisect index."""

    def __init__(self, bars: list[dict]):
        self.bars, self.dates = bars, [b["date"] for b in bars]

    def first_after(self, d: str) -> int:  # first bar with date > d
        return bisect.bisect_right(self.dates, d)

    def first_on_or_after(self, d: str) -> int:
        return bisect.bisect_left(self.dates, d)

    def last_up_to(self, d: str) -> int:  # last bar with date <= d, or -1
        return bisect.bisect_right(self.dates, d) - 1


def _add_days(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def run_claims_backtest(request: dict, *, now: datetime | None = None) -> dict:
    """Pure: request -> response dict for every status; never raises on bad input."""
    now = now or datetime.now(UTC)
    if errors := validate_request_v1(request):
        return _response("invalid", None, now, errors=errors)
    rsha = request_sha256(request)
    try:
        return _run(request, rsha, now)
    except Exception as e:  # never raise across the contract boundary
        return _response("error", rsha, now, errors=[{"code": "engine_error", "message": f"{type(e).__name__}: {e}"}])


def _run(req: dict, rsha: str, now: datetime) -> dict:
    pb, pf, as_of, mode = req["playbook"], req["portfolio"], _t(req["as_of"]), req["mode"]
    hind = mode == "hindsight_said_at"
    trig_field = "said_at" if hind else "known_at"
    raw = req["prices"]["inline"]["bars"]
    fbs = req["factblocks"]
    if as_of > now:
        return _response("refused", rsha, now, errors=[{"code": "as_of_in_future", "field": "as_of",
                         "message": f"as_of {req['as_of']} is after the server clock {_iso(now)}.",
                         "detail": {"as_of": req["as_of"], "now": _iso(now)}}])
    bench_sym = pb["benchmark"]["symbol"]
    if bench_sym not in raw:
        return _response("invalid", rsha, now, errors=[{"code": "invalid_request", "field": "prices.inline.bars",
                         "message": f"No bars for the benchmark {bench_sym}."}])

    # Market of each symbol: from the calls; the benchmark trades in US hours.
    market = {bench_sym: "US"}
    for f in fbs:
        if f["call"]["symbol"]:
            market.setdefault(f["call"]["symbol"], f["call"]["market"] or "US")
    series, masked = {}, []
    for sym in sorted(raw):
        known = [b for b in raw[sym] if _close_at(b["date"], market.get(sym, "US")) <= as_of]
        late = [b for b in raw[sym] if _close_at(b["date"], market.get(sym, "US")) > as_of]
        if late:
            masked.append({"input": "prices.bars", "symbol": sym, "from": late[0]["date"], "to": late[-1]["date"],
                           "count": len(late)})
        series[sym] = _Series(known)
    spy = series[bench_sym]

    # Pass 1: which FactBlocks trade and where they enter (claims_v1 pass 1, ordered by trigger).
    order = sorted(fbs, key=lambda f: (_t(f[trig_field]), f["id"]))
    skipped, entries, same_day, not_yet = [], {}, {}, []
    used_before_known = []
    for f in order:
        c = f["call"]
        if _t(f[trig_field]) > as_of:
            not_yet.append(f["id"])
            skipped.append({"factblock_id": f["id"], "reason": "not_known_at_as_of" if not hind else "not_said_at_as_of"})
            continue
        if not c["symbol"]:
            skipped.append({"factblock_id": f["id"], "reason": "not_investable"})
            continue
        if c["direction"] not in ("up", "down"):
            skipped.append({"factblock_id": f["id"], "reason": "neutral"})
            continue
        if c["symbol"] not in series:
            skipped.append({"factblock_id": f["id"], "reason": "no_prices"})
            continue
        s, mk = series[c["symbol"]], c["market"] or "US"
        idx = s.first_after(_local_date(f[trig_field], mk))
        if idx >= len(s.bars):
            skipped.append({"factblock_id": f["id"], "reason": "no_entry_bar"})
            continue
        # One call per speaker, symbol and said day; same-day opposite calls cancel (claims_v1).
        key = (f["speaker"]["id"], c["symbol"], _local_date(f["said_at"], mk))
        if (seen := same_day.get(key)) is not None:
            if seen["direction"] == c["direction"]:
                skipped.append({"factblock_id": f["id"], "reason": "duplicate_same_day"})
                continue
            for i in seen["ids"]:
                entries.pop(i, None)
                skipped.append({"factblock_id": i, "reason": "conflicting_same_day"})
            seen.update(ids=[], direction="conflict")
            skipped.append({"factblock_id": f["id"], "reason": "conflicting_same_day"})
            continue
        same_day[key] = {"direction": c["direction"], "ids": [f["id"]]}
        entries[f["id"]] = idx
    if not_yet:
        masked.insert(0, {"input": "factblocks", "field": trig_field, "count": len(not_yet), "ids": not_yet})

    # Pass 2: exits. A later opposite call by the same speaker on the same symbol closes at its own entry.
    by_pair: dict[tuple, list[dict]] = {}
    for f in order:
        if f["id"] in entries:
            by_pair.setdefault((f["speaker"]["id"], f["call"]["symbol"]), []).append(f)
    cost = 2 * pb["cost_bps"] / 10_000
    trades, backfilled = [], False
    for f in order:
        if f["id"] not in entries:
            continue
        c, idx = f["call"], entries[f["id"]]
        s = series[c["symbol"]]
        entry = s.bars[idx]
        h = c["horizon_days"]
        days, explicit = (min(pb["max_horizon_days"], h), True) if h and h > 0 else (pb["default_horizon_days"], False)
        h_idx = s.first_on_or_after(_add_days(entry["date"], days))
        exit_idx, reason, closed_by = (h_idx if h_idx < len(s.bars) else -1), "horizon", None
        if pb["exit_on_opposite_call"]:
            for o in by_pair[(f["speaker"]["id"], c["symbol"])]:
                if _t(o[trig_field]) > _t(f[trig_field]) and o["call"]["direction"] != c["direction"] \
                        and entries[o["id"]] >= idx:
                    if exit_idx < 0 or entries[o["id"]] < exit_idx:
                        exit_idx, reason, closed_by = entries[o["id"]], "opposite_call", o["id"]
                    break
        d = 1 if c["direction"] == "up" else -1
        last = len(s.bars) - 1
        t = {"factblock_id": f["id"], "speaker_id": f["speaker"]["id"], "symbol": c["symbol"],
             "market": c["market"] or "US", "direction": c["direction"], "trigger": trig_field,
             "trigger_at": f[trig_field], "said_at": f["said_at"], "known_at": f["known_at"],
             "horizon_days": days, "horizon_explicit": explicit,
             "entry": {"date": entry["date"], "price": entry["open"]}, "exit": None, "exit_reason": "pending",
             "closed_by": None, "mark": {"date": s.bars[last]["date"], "price": s.bars[last]["close"]},
             "ret": None, "benchmark_raw_ret": None, "benchmark_ret": None, "excess": None, "hit": None}
        if exit_idx >= 0:
            ex = s.bars[exit_idx]
            move = ex["close"] / entry["open"] - 1
            signed = d * move
            if pb["short_loss_cap"] is not None and signed < -pb["short_loss_cap"]:
                signed = -pb["short_loss_cap"]
            i, j = spy.first_on_or_after(entry["date"]), spy.last_up_to(ex["date"])
            raw_b = spy.bars[j]["close"] / spy.bars[i]["open"] - 1 if i < len(spy.bars) and j >= i else None
            bret = None if raw_b is None else d * raw_b if pb["benchmark_matches_direction"] else raw_b
            t.update(exit={"date": ex["date"], "price": ex["close"]}, exit_reason=reason, closed_by=closed_by,
                     ret=signed - cost, benchmark_raw_ret=raw_b, benchmark_ret=bret,
                     excess=None if bret is None else signed - cost - bret, hit=1 if d * move > 0 else 0)
        if hind and _t(f["known_at"]) > _open_at(entry["date"], t["market"]):
            # traded at an open before the ledger knew the call
            used_before_known.append({"factblock_id": f["id"], "known_at": f["known_at"], "used_at": _iso(_open_at(entry["date"], t["market"]))})
        backfilled |= f["backfilled"]
        trades.append(t)

    reasons = (["hindsight_said_at"] if hind else []) + (["backfilled_known_at"] if backfilled else [])
    cert = {"as_of": req["as_of"], "mode": mode, "engine_version": ENGINE_VERSION, "mock": False,
            "request_sha256": rsha, "prices_sha256": sha256(req["prices"]["inline"]),
            "playbook_sha256": sha256(pb), "factblocks_sha256": sha256(fbs),
            "calendar": {"id": "bars", "note": "sessions are the dates of the bars given"},
            "hindsight": hind, "backfilled": backfilled, "reproducible": not reasons,
            "not_reproducible_because": reasons, "masked": masked, "used_before_known": used_before_known,
            "assumptions": ASSUMPTIONS}
    return _response("ok" if trades else "no_trade", rsha, now, trades=trades, skipped=skipped,
                     summary=_summary(trades, pf), certificate=cert)


def _summary(trades: list[dict], pf: dict) -> dict:
    """Unit-bet portfolio (claims_v1 buildEntries/summarizeClaimsV1): resolved trades in exit order, each adds
    unit x ret; open trades are not marked into the curve. Returns are fractions of initial capital."""
    cap, unit = pf["initial_capital"], pf["initial_capital"] * pf["unit_fraction"]
    done = sorted((t for t in trades if t["ret"] is not None), key=lambda t: (t["exit"]["date"], t["factblock_id"]))
    pnl = bench = 0.0
    peak, mdd, curve = cap, 0.0, []
    for t in done:
        pnl += unit * t["ret"]
        bench += unit * (t["benchmark_ret"] or 0)
        v = cap + pnl
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
        curve.append({"date": t["exit"]["date"], "factblock_id": t["factblock_id"], "value": v, "benchmark": cap + bench})
    ex = [t["excess"] for t in done if t["excess"] is not None]
    return {"n_trades": len(trades), "n_resolved": len(done), "n_pending": len(trades) - len(done),
            "excess_mean": sum(ex) / len(ex) if ex else None,
            "hit_rate": sum(t["hit"] for t in done) / len(done) if done else None,
            "total_return": pnl / cap, "benchmark_return": bench / cap, "max_drawdown": mdd,
            "initial_capital": cap, "unit": unit, "curve": curve}
