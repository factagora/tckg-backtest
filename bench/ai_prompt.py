"""Baseline: ask an LLM to do the io-v0 backtest by hand from the same request JSON, calendar and rules.

    python3 bench/ai_prompt.py --model claude-sonnet-5-5 --runs 5
    python3 bench/ai_prompt.py --dry-run          # print the prompt for request 01, no API call

Needs ANTHROPIC_API_KEY. Writes bench/results/<UTC timestamp>-ai-<model>.json with the prompt, raw text, parsed
answer, score, wall time, token usage and cost estimate of every call. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import NAMES, calendar, fingerprint, load, score, summarize  # noqa: E402

API = "https://api.anthropic.com/v1/messages"
MAX_TOKENS = 16000
# Assumed list prices, USD per million tokens (input, output): Anthropic first-party rates as of 2026-10-06 (claude-api
# skill model table). Not read from an API; edit if the price sheet differs. Thinking tokens bill as output.
PRICE_PER_MTOK = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}

INSTRUCTION = """Backtest the trading rule in the request below by hand, using only the data given. Rules:

1. Signal time (trigger_at). Mode "as_known": trigger event "known_at" fires at factblock.known_at; trigger event
   "verdict_decided" fires at the later of factblock.known_at and factblock.verdict.decided_at, and only if
   verdict.value is in trigger.verdict_in and decided_at <= as_of. Mode "hindsight_said_at": trigger event "known_at"
   fires at factblock.said_at.
2. Sessions. XKRX sessions are weekdays that are not in the calendar's holidays. A session opens at 09:00 and closes
   at 15:30 Asia/Seoul (00:00 and 06:30 UTC).
3. Entry. Buy at the open of the first session that opens strictly after trigger_at.
4. Exit. Count playbook.hold.sessions sessions after the entry session; sell at that session's open (exit.at "open").
5. Returns. ret = exit price / entry price - 1. benchmark_ret: the same for playbook.benchmark at the same sessions and
   the same price type. excess = ret - benchmark_ret. Do not round.
6. No information after as_of. An open price is known from 09:00 Asia/Seoul of its session, a close from 15:30. If the
   exit open is after as_of, the trade is open: give the planned exit date, null exit prices, open true, and mark it
   at the close of the last session that closed at or before as_of (stock and benchmark); then ret and benchmark_ret
   use the mark instead of the exit. For a closed trade, marked_at and the mark prices are null.

End your answer with exactly one JSON object in a ```json code block, with these keys:
{"entry_date": "YYYY-MM-DD", "entry_price": number, "entry_benchmark_price": number,
 "exit_date": "YYYY-MM-DD", "exit_price": number or null, "exit_benchmark_price": number or null,
 "open": boolean, "marked_at": "YYYY-MM-DD" or null, "mark_price": number or null,
 "mark_benchmark_price": number or null, "ret": number, "benchmark_ret": number, "excess": number}
"""


def prompt(request: dict) -> str:
    return (INSTRUCTION + "\nXKRX calendar:\n" + json.dumps(calendar()) +
            "\n\nRequest:\n" + json.dumps(request, indent=2) + "\n")


def parse(text: str) -> dict | None:
    """The last ```json block, else the whole text, as a JSON object; None if neither parses."""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    for s in ([blocks[-1]] if blocks else []) + [text.strip()]:
        try:
            x = json.loads(s)
        except ValueError:
            continue
        if isinstance(x, dict):
            return x
    return None


def call(model: str, text: str, key: str) -> tuple[dict, float]:
    body = json.dumps({"model": model, "max_tokens": MAX_TOKENS,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{model}: HTTP {e.code} {e.read()[:300]!r}")
    return out, time.perf_counter() - t0


def cost(model: str, usage: dict) -> float | None:
    p = PRICE_PER_MTOK.get(model)
    return None if p is None else (usage["input_tokens"] * p[0] + usage["output_tokens"] * p[1]) / 1e6


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--only", choices=NAMES, action="append", help="request name; repeatable (default: 01-03)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    names = a.only or list(NAMES)
    if a.dry_run:
        print(prompt(load(names[0])[0]))
        return 0
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    out = {"kind": "ai", "model": a.model, "max_tokens": MAX_TOKENS, "temperature": "API default", "thinking": "API default (adaptive)", "effort": "API default",
           "price_per_mtok_usd_assumed": PRICE_PER_MTOK.get(a.model), "instruction": INSTRUCTION,
           "started_at": datetime.now(UTC).isoformat(timespec="seconds"), "requests": {}}
    for name in names:
        request, golden = load(name)
        text, runs = prompt(request), []
        for i in range(a.runs):
            try:
                resp, wall = call(a.model, text, key)
            except (SystemExit, OSError) as e:  # keep the runs already paid for; an API error counts as a parse failure
                resp, wall = {"content": [{"type": "text", "text": f"API error: {e}"}], "usage": None}, float("nan")
            raw = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            ans = parse(raw)
            runs.append({"run": i + 1, "wall_s": wall, "model_returned": resp.get("model"), "usage": resp.get("usage"),
                         "stop_reason": resp.get("stop_reason"), "cost_usd": cost(a.model, resp["usage"]) if resp["usage"] else None,
                         "answer": ans, "answer_key": fingerprint(ans) if ans is not None else "parse_fail:" + fingerprint(raw),
                         "score": score(ans, request, golden), "raw_text": raw})
            print(f"{name} run {i + 1}: {wall:.1f}s {runs[-1]['score']}", flush=True)
        out["requests"][name] = {"prompt": text, "summary": summarize(runs), "runs": runs}
    path = Path(__file__).resolve().parent / "results" / \
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-ai-{a.model}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
