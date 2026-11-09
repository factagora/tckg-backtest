"""Read a FactBlock from hosted TCKG and build a tckg-backtest request (io-v0 section 2) for it.

TCKG is Factagora's hosted temporal causal knowledge graph; it stores FactBlocks (github.com/factagora/factblock) and
stamps known_at server-side. This module only calls its HTTP API; no TCKG code is in this repo.

    python -m tckg_backtest.sources.tckg [--id ID --space SPACE] [--prices P.csv] [--mode M] [--as-of T] [--run]

Env: TCKG_URL (default https://tckg.factagora.com), TCKG_API_KEY (never printed). Without TCKG_API_KEY the
SK hynix FactBlock fixture (fixtures/factblock-skhynix.json) is used instead, and the output says so.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tarfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime, time
from pathlib import Path

from tckg_backtest.engine import KST, _sessions_after, _t

URL = "https://tckg.factagora.com"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACTBLOCK_FIXTURE = FIXTURES / "factblock-skhynix.json"
PRICES_FIXTURE = FIXTURES / "prices-000660-069500.csv"
AS_OF = "2026-11-08T00:00:00Z"   # io-v0 8: the demo as_of
CLAIM_FOLLOW_20 = {"id": "claim-follow-20", "version": 1, "name": "Follow the claim from when it is known, hold 20 sessions",
                   "trigger": {"event": "known_at"}, "entry": {"at": "first_open_after"}, "hold": {"sessions": 20},
                   "exit": {"at": "open"}, "direction": "long", "benchmark": {"symbol": "069500", "market": "XKRX"}}


def _call(method: str, path: str, key: str, body: dict | None = None, **params) -> dict:
    url = os.environ.get("TCKG_URL", URL).rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _utc(s: str) -> str:
    """UTC with Z; sub-seconds kept (truncating a known_at would make the block look known earlier than it was)."""
    t = _t(s).astimezone(UTC)
    return t.isoformat(timespec="microseconds" if t.microsecond else "seconds").replace("+00:00", "Z")


def _export(space: str, as_of: str, key: str) -> dict[str, list[dict]]:
    """GET /v1/export: the space as known at as_of, {table: rows} from a tar.gz of JSONL (nodes, edges, resolutions)."""
    url = os.environ.get("TCKG_URL", URL).rstrip("/") + "/v1/export?" + urllib.parse.urlencode({"as_of": as_of, "space": space})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"authorization": f"Bearer {key}"}), timeout=120) as r:
        data = r.read()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return {Path(m.name).stem: [json.loads(x) for x in t.extractfile(m).read().decode().splitlines() if x.strip()]
                for m in t.getmembers() if m.isfile() and m.name.endswith(".jsonl")}


def fetch_factblock(block_id: str, space: str, as_of: str, key: str) -> dict:
    """The contract factblock (io-v0 2.2) for a TCKG node, read as known at as_of. GET /v1/memories/{id} is a
    valid-time read (no_value_at for a commitment) and carries no verdicts, so the space export is read instead."""
    ex = _export(space, as_of, key)
    node = next((n for n in ex.get("nodes", []) if n["id"] == block_id), None)
    if node is None:
        raise SystemExit(f"{block_id}: not in TCKG space {space} as of {as_of}")
    seen = _call("POST", "/v1/memories/lookup", key, body={"ids": [block_id], "space": space})["items"][0]
    p = node.get("payload") or {}
    speaker = p.get("speaker", "")
    # verdict.decided_at is when the ledger recorded it (io-v0 2.2): the resolution row's known_at
    res = sorted((r for r in ex.get("resolutions", []) if r["target_id"] == block_id), key=lambda r: _t(r["known_at"]))
    v = res[-1] if res else None
    return {"id": node["id"], "kind": node["kind"], "statement": node["statement"],
            "speaker": speaker if isinstance(speaker, dict) else {"id": p.get("speaker_id", speaker), "name": speaker},
            "said_at": _utc(node["asserted_at"]), "said_at_precision": p.get("asserted_at_precision", "instant"),
            "known_at": _utc(node["known_at"]), "backfilled": bool(seen.get("backfill_batch")),
            "verdict": {"value": v["outcome"], "decided_at": _utc(v["known_at"])} if v and v["outcome"] in ("hit", "miss") else None,
            # v0 is XKRX only: other markets are left out by name (io-v0 2.2), main() lists them
            "assets": [{"symbol": a.split(":", 1)[1], "market": "XKRX"} for a in p.get("assets", []) if a.startswith("krx:")],
            "_left_out": [a for a in p.get("assets", []) if not a.startswith("krx:")]}


def _num(s: str):
    x = float(s)
    return int(x) if x.is_integer() else x


def request(fb: dict, playbook: dict, prices: Path, *, mode: str = "as_known", as_of: str = AS_OF) -> dict:
    """A contract request with prices.inline bars over io-v0 2.1's window (entry through planned exit, opened by as_of)."""
    trig = playbook["trigger"]
    v = fb["verdict"]
    if trig["event"] == "known_at":
        trigger_at = fb["said_at" if mode == "hindsight_said_at" else "known_at"]
    elif mode == "as_known" and v and v["value"] in trig["verdict_in"] and _t(v["decided_at"]) <= _t(as_of):
        trigger_at = max(fb["known_at"], v["decided_at"], key=_t)
    else:
        trigger_at = None   # no signal (or hindsight x verdict): every key gets an empty list
    days = set()
    if trigger_at and _t(trigger_at) <= _t(as_of):
        days = {d.isoformat() for d in _sessions_after(_t(trigger_at), playbook["hold"]["sessions"])}
    symbols = sorted({a["symbol"] for a in fb["assets"]} | {playbook["benchmark"]["symbol"]})
    bars = {s: {} for s in symbols}
    with open(prices, newline="") as f:
        for r in csv.DictReader(f):
            if r["symbol"] not in bars or r["date"] not in days or float(r["open"]) <= 0 or float(r["close"]) <= 0:
                continue
            d = datetime.fromisoformat(r["date"])
            open_at = r.get("open_at") or datetime.combine(d, time(9), KST).strftime("%Y-%m-%dT%H:%M:%S%z")
            if _t(open_at) > _t(as_of):
                continue
            bar = {"date": r["date"], "open": _num(r["open"]), "close": _num(r["close"])}
            # shortcut: the fixture writes open_at/close_at only when they differ from the calendar default (io-v0 2.1)
            bar |= {k: _utc(r[k]) for k in ("open_at", "close_at") if r.get(k)}
            bars[r["symbol"]][r["date"]] = bar
    return {"contract": "tckg-backtest-io/v0", "mode": mode, "as_of": as_of, "factblock": fb, "playbook": playbook,
            "prices": {"inline": {"currency": "KRW", "source": "gs://tckg-trader-data/daily/prices.csv (KIS daily bars, unadjusted)",
                                  "bars": {s: [b[d] for d in sorted(b)] for s, b in bars.items()}}},
            "calendar": {"id": "XKRX"}}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", help="TCKG node id (needs TCKG_API_KEY)")
    ap.add_argument("--space", default="theme:kr-memory-semis")
    ap.add_argument("--prices", type=Path, default=PRICES_FIXTURE, help="CSV: symbol,date,open,close[,open_at,close_at]")
    ap.add_argument("--playbook", type=Path, help="Playbook JSON (default claim-follow-20)")
    ap.add_argument("--mode", default="as_known", choices=["as_known", "hindsight_said_at"])
    ap.add_argument("--as-of", default=AS_OF)
    ap.add_argument("--run", action="store_true", help="run the engine and print the response, not the request")
    a = ap.parse_args(argv)
    key = os.environ.get("TCKG_API_KEY")
    if key and a.id:
        fb = fetch_factblock(a.id, a.space, a.as_of, key)
        left = fb.pop("_left_out")
        print(f"factblock {a.id} read from TCKG ({os.environ.get('TCKG_URL', URL)}, space {a.space}, as_of {a.as_of})"
              + (f"; left out (not XKRX): {', '.join(left)}" if left else ""), file=sys.stderr)
    else:
        fb = json.loads(FACTBLOCK_FIXTURE.read_text())
        print("factblock from the checked-in fixture (no TCKG_API_KEY or --id): SK hynix, "
              f"{FACTBLOCK_FIXTURE.name}", file=sys.stderr)
    pb = json.loads(a.playbook.read_text()) if a.playbook else CLAIM_FOLLOW_20
    req = request(fb, pb, a.prices, mode=a.mode, as_of=a.as_of)
    if a.run:
        from tckg_backtest.engine import run_backtest
        req = run_backtest(req)
    json.dump(req, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
