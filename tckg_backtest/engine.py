"""The v0 engine: one FactBlock x one Playbook rule -> trades, summary, certificate (io-v0 sections 4 and 5)."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from functools import cache
from typing import Callable

from tckg_backtest.canonical import request_sha256, sha256
from tckg_backtest.core import ENGINE_VERSION, _iso, _json, response, validate_request

KST = timezone(timedelta(hours=9))
ASSUMPTIONS = ["gross of costs and taxes", "no dividends", "filled at the session's open or close price",
               "unadjusted prices; an open is known from open_at and a close from close_at"]


class Refusal(Exception):
    def __init__(self, *errors: dict):
        self.errors = list(errors)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@cache
def _calendar():
    cal = _json("calendar-XKRX.json")
    hol = frozenset(date.fromisoformat(d) for d in cal["holidays"])
    first, last = date(int(cal["holidays"][0][:4]), 1, 1), date(int(cal["holidays"][-1][:4]), 12, 31)
    return cal, sha256(cal), hol, first, last


def _default(d: date, side: str) -> datetime:
    cal = _calendar()[0]
    # Later: late-open sessions take open_at from the bar (the session pick below uses the default open)
    return datetime.combine(d, time.fromisoformat(cal[side]), KST).astimezone(UTC)


def _sessions_after(trigger: datetime, n: int) -> list[date]:
    """The first session opening strictly after trigger and the n sessions after it."""
    _, _, hol, first, last = _calendar()
    out, d = [], trigger.astimezone(KST).date()
    while len(out) <= n:
        if not first <= d <= last:
            raise Refusal({"code": "calendar_out_of_range", "message": f"{d} is outside the XKRX table ({first} to {last}).",
                           "detail": {"date": d.isoformat()}})
        if d.weekday() < 5 and d not in hol and (out or _default(d, "open") > trigger):
            out.append(d)
        d += timedelta(days=1)
    return out


def _bar_time(bar: dict, side: str) -> datetime:
    return _t(bar[f"{side}_at"]) if f"{side}_at" in bar else _default(date.fromisoformat(bar["date"]), side)


def _known(bars: dict, sym: str, d: date, side: str, as_of: datetime) -> bool:
    """Whether the side's price of session d was known by as_of (from the bar's time, else the calendar default)."""
    bar = bars.get(sym, {}).get(d.isoformat())
    return (_bar_time(bar, side) if bar else _default(d, side)) <= as_of


def _price(bars: dict, sym: str, d: date, side: str, as_of: datetime) -> tuple[float, str]:
    bar = bars.get(sym, {}).get(d.isoformat())
    if bar is None or _bar_time(bar, side) > as_of:
        raise Refusal({"code": "missing_price_bar", "message": f"No {side} price for {sym} on {d} as of the request.",
                       "detail": {"symbol": sym, "date": d.isoformat(), "side": side}})
    return bar[side], _iso(_bar_time(bar, side))


def _masked(fb: dict, raw_bars: dict, as_of: datetime) -> list[dict]:
    out = []
    if (v := fb["verdict"]) and _t(v["decided_at"]) > as_of:
        out.append({"input": "factblock.verdict", "known_at": v["decided_at"]})
    for sym in sorted(raw_bars):
        for side, key in (("open", "prices.bars"), ("close", "prices.close")):
            # bars: opened after as_of; close: opened by as_of but closed after it
            days = [b["date"] for b in raw_bars[sym] if _bar_time(b, side) > as_of
                    and (side == "open" or _bar_time(b, "open") <= as_of)]
            if days:
                out.append({"input": key, "symbol": sym, "from": min(days), "to": max(days), "count": len(days)})
    return out


def _trigger(req: dict, as_of: datetime) -> tuple[str | None, str | None]:
    """(trigger_at string, field) per io-v0 4.1; (None, None) when a verdict trigger is not met as of as_of."""
    fb, trig, hind = req["factblock"], req["playbook"]["trigger"], req["mode"] == "hindsight_said_at"
    if trig["event"] == "verdict_decided" and hind:
        raise Refusal({"code": "unsupported_combination", "field": "playbook.trigger",
                       "message": "hindsight_said_at with a verdict_decided trigger is not supported in v0."})
    field = "said_at" if hind else "known_at"
    if _t(fb[field]) > as_of:
        what = "said" if hind else "in the ledger"
        raise Refusal({"code": "trigger_after_as_of", "field": f"factblock.{field}",
                       "message": f"The trigger ({field} {fb[field]}) is after as_of {req['as_of']}: "
                                  f"the FactBlock was not {what} yet.",
                       "detail": {"trigger_at": fb[field], "as_of": req["as_of"]}})
    if trig["event"] == "known_at":
        return fb[field], field
    v = fb["verdict"]
    if not v or v["value"] not in trig["verdict_in"] or _t(v["decided_at"]) > as_of:
        return None, None
    return max(fb["known_at"], v["decided_at"], key=_t), "verdict.decided_at"


def _note(req: dict, trigger_at: str, exit_: dict, marked_at: str | None, side: str) -> str:
    fb, parts = req["factblock"], []
    if req["mode"] == "hindsight_said_at":
        day = (" (day precision, taken at the start of the speaker's local day)"
               if fb["said_at_precision"] == "day" else "")
        parts.append(f"Hindsight: entered as if the FactBlock were known when said{day}; "
                     f"the ledger had it from {fb['known_at']}.")
    elif req["playbook"]["trigger"]["event"] == "verdict_decided":
        parts.append(f"Entry waits for the ledger's verdict ({trigger_at}), not the disclosure time.")
    if marked_at:
        parts.append(f"Open: the exit is planned at the {exit_['date']} {side}; "
                     f"marked at the {marked_at} close, the last close at or before as_of.")
    return " ".join(parts) or f"Closed at the {exit_['date']} {side}."


def _trade(req, sym, bench, trigger_at, days, bars, as_of) -> dict:
    side = req["playbook"]["exit"]["at"]
    entry_d, exit_d = days[0], days[-1]
    p0, at0 = _price(bars, sym, entry_d, "open", as_of)
    b0, _ = _price(bars, bench, entry_d, "open", as_of)
    exit_ = {"date": exit_d.isoformat(), "at": _iso(_default(exit_d, side)), "price": None, "benchmark_price": None,
             "planned": True}
    mark = marked_at = None
    if _known(bars, sym, exit_d, side, as_of) and _known(bars, bench, exit_d, side, as_of):
        p1, exit_["at"] = _price(bars, sym, exit_d, side, as_of)
        b1, _ = _price(bars, bench, exit_d, side, as_of)
        exit_.update(price=p1, benchmark_price=b1, planned=False)
    else:
        closed = [d for d in days if _known(bars, sym, d, "close", as_of) and _known(bars, bench, d, "close", as_of)]
        if closed:
            p1, at1 = _price(bars, sym, closed[-1], "close", as_of)
            b1, _ = _price(bars, bench, closed[-1], "close", as_of)
            marked_at, mark = closed[-1].isoformat(), {"at": at1, "price": p1, "benchmark_price": b1}
        else:
            # shortcut: no close since entry yet (as_of inside the entry session); marked flat at the entry open
            p1, b1 = p0, b0
            marked_at, mark = entry_d.isoformat(), {"at": at0, "price": p0, "benchmark_price": b0}
    ret, bret = p1 / p0 - 1, b1 / b0 - 1
    return {"symbol": sym, "market": "XKRX", "benchmark": bench, "direction": "long",
            "entry_basis": "said" if req["mode"] == "hindsight_said_at" else "known",
            "trigger": req["playbook"]["trigger"]["event"], "trigger_at": trigger_at,
            "entry": {"date": entry_d.isoformat(), "at": at0, "price": p0, "benchmark_price": b0},
            "exit": exit_, "open": mark is not None, "marked_at": marked_at, "mark": mark,
            "ret": ret, "benchmark_ret": bret, "excess": ret - bret,
            "note": _note(req, trigger_at, exit_, marked_at, side)}


def run_backtest(request: dict, *, load_prices: Callable[[str], bytes] | None = None,
                 now: datetime | None = None) -> dict:
    """Pure: request -> response dict for every status; never raises on bad input. now: only for as_of_in_future."""
    now = now or datetime.now(UTC)
    if errors := validate_request(request):
        return response("invalid", None, errors=errors, now=now)
    rsha = request_sha256(request)
    try:
        return _run(request, rsha, load_prices, now)
    except Exception as e:  # a bug, not bad input
        return response("error", rsha, now=now, errors=[{"code": "engine_error", "message": f"{type(e).__name__}: {e}"}])


def _run(req: dict, rsha: str, load_prices, now: datetime) -> dict:
    fb, pb, as_of = req["factblock"], req["playbook"], _t(req["as_of"])
    cal, cal_sha = _calendar()[:2]
    if "ref" in req["prices"]:
        # shortcut: prices.ref (gs:// CSV) is io-v0 10 Stage C, not built yet; upgrade when a caller sends refs
        return response("error", rsha, now=now, errors=[{"code": "prices_unavailable", "field": "prices.ref",
                        "message": "This deployment does not read prices.ref yet; send prices.inline."}])
    if req["calendar"].get("sha256") not in (None, cal_sha):
        return response("invalid", rsha, now=now, errors=[{"code": "invalid_request", "field": "calendar.sha256",
                        "message": f"calendar.sha256 does not match the engine's XKRX table ({cal_sha})."}])
    raw_bars = req["prices"]["inline"]["bars"]
    bars = {s: {b["date"]: b for b in bs} for s, bs in raw_bars.items()}
    hind = req["mode"] == "hindsight_said_at"
    reasons = (["hindsight_said_at"] if hind else []) + (["backfilled_known_at"] if fb["backfilled"] else [])
    cert = {"as_of": req["as_of"], "mode": req["mode"], "engine_version": ENGINE_VERSION, "mock": False,
            "request_sha256": rsha, "prices_sha256": sha256(req["prices"]["inline"]), "playbook_sha256": sha256(pb),
            "calendar": {"id": cal["id"], "sha256": cal_sha}, "hindsight": hind, "backfilled": fb["backfilled"],
            "reproducible": not reasons, "not_reproducible_because": reasons,
            "masked": _masked(fb, raw_bars, as_of), "used_before_known": [], "assumptions": ASSUMPTIONS}

    def out(status, **kw):
        return response(status, rsha, certificate=cert, now=now, **kw)

    try:
        if as_of > now:
            raise Refusal({"code": "as_of_in_future", "field": "as_of",
                           "message": f"as_of {req['as_of']} is after the server clock {_iso(now)}.",
                           "detail": {"as_of": req["as_of"], "now": _iso(now)}})
        if off := [{"code": "market_not_on_calendar", "message": f"{a['symbol']} is on {a['market']}, not XKRX.",
                    "detail": {"symbol": a["symbol"], "market": a["market"]}}
                   for a in [*fb["assets"], pb["benchmark"]] if a["market"] != cal["id"]]:
            raise Refusal(*off)
        trigger_at, _ = _trigger(req, as_of)
        if trigger_at is None:
            return out("no_trade", no_trade_reason="trigger_not_met")
        days = _sessions_after(_t(trigger_at), pb["hold"]["sessions"])
        if not _known(bars, pb["benchmark"]["symbol"], days[0], "open", as_of):
            return out("no_trade", no_trade_reason="entry_after_as_of")
        if hind and _t(trigger_at) < _t(fb["known_at"]):
            cert["used_before_known"] = [{"input": "factblock", "known_at": fb["known_at"], "used_at": trigger_at}]
        trades, errors = [], []
        for a in fb["assets"]:
            try:
                trades.append(_trade(req, a["symbol"], pb["benchmark"]["symbol"], trigger_at, days, bars, as_of))
            except Refusal as r:
                errors += r.errors
        if errors:
            raise Refusal(*errors)
    except Refusal as r:
        return out("refused", errors=r.errors)
    n = len(trades)
    summary = {"n_trades": n, "n_closed": sum(not t["open"] for t in trades), "n_open": sum(t["open"] for t in trades),
               "ret_mean": sum(t["ret"] for t in trades) / n,
               "benchmark_ret_mean": sum(t["benchmark_ret"] for t in trades) / n,
               "excess_mean": sum(t["excess"] for t in trades) / n, "portfolio": None}
    return out("ok", trades=trades, summary=summary)
