"""Time experiment: an analyst track record "as of T" from the hosted TCKG ledger vs an LLM given the same rows.

    TCKG_API_KEY=... python3 bench/time_ledger.py --model claude-sonnet-5-5 --arm A --runs 5

Ledger rows: theme:kr-memory-semis predictions and claims with their verdict resolutions, exported as known NOW.
- arm A: the LLM gets the TCKG rows (said_at, known_at, verdict recorded_at) and must filter to T itself.
- arm B: the LLM gets what an overwrite-style sheet holds: said_at and the current outcome, no ledger times.
Ground truth is an oracle over the NOW export (node known_at <= T and resolution known_at <= T), independent of the
engine; it is also checked against TCKG's own export at as_of=T. The ledger data itself is not written to this repo
(TCKG is private); results keep ids, outcomes and scores only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from ai_prompt import call_cli, cost, parse  # noqa: E402
from tckg_backtest.sources.tckg import _export  # noqa: E402

SPACE = "theme:kr-memory-semis"
T = "2026-10-27T00:00:00Z"  # before SK hynix (10-28), MSFT and Samsung Q3 results; after Meta's (10-21)
KINDS = ("prediction", "claim")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def speaker(n: dict) -> str:
    s = (n.get("payload") or {}).get("speaker", "")
    return s.get("name") or s.get("id", "") if isinstance(s, dict) else str(s)


def oracle(ex: dict, t: str) -> dict[str, str]:
    """{block id: outcome} for blocks known by t whose verdict was recorded by t. No TCKG or engine code."""
    known = {n["id"] for n in ex["nodes"] if n["kind"] in KINDS and ts(n["known_at"]) <= ts(t)}
    return {r["target_id"]: r["outcome"] for r in ex["resolutions"]
            if r["target_id"] in known and ts(r["known_at"]) <= ts(t) and r["outcome"] in ("hit", "miss")}


def rows(ex: dict, arm: str) -> list[dict]:
    res = {r["target_id"]: r for r in ex["resolutions"]}
    out = []
    for n in sorted((n for n in ex["nodes"] if n["kind"] in KINDS), key=lambda n: n["id"]):
        r = res.get(n["id"])
        row = {"id": n["id"], "kind": n["kind"], "speaker": speaker(n), "statement": n["statement"],
               "said_at": n["asserted_at"]}
        if arm == "A":
            row["known_at"] = n["known_at"]
            row["verdict"] = {"outcome": r["outcome"], "recorded_at": r["known_at"]} if r else None
        else:
            row["outcome"] = r["outcome"] if r else None
        out.append(row)
    return out


PROMPT = """Below are analyst and company statements (predictions and claims) about memory semiconductors{what}.

Question: as of {t}, which statements already had a verdict, and what was it? Use only what was known at {t};
do not use any information from after {t}.

End your answer with exactly one JSON object in a ```json code block:
{{"resolved": {{"<statement id>": "hit" or "miss", ...}}}}
List only statements whose verdict was known at {t}.

Statements:
{rows}
"""
WHAT = {"A": ", exported from a ledger: said_at is when it was said, known_at is when the ledger recorded it, "
             "verdict.recorded_at is when the verdict was recorded",
        "B": ", from a spreadsheet: said_at is when it was said, outcome is the verdict as it stands today"}


def score(ans, truth: dict, now_outcomes: dict) -> dict:
    if not isinstance(ans, dict) or not isinstance(ans.get("resolved"), dict):
        return {"parsed": False, "correct": False, "leaked": None, "missed": None, "wrong_outcome": None}
    got = ans["resolved"]
    leaked = sorted(i for i in got if i not in truth and i in now_outcomes)  # decided only after T
    return {"parsed": True, "correct": got == truth, "leaked": len(leaked), "leaked_ids": leaked,
            "missed": len([i for i in truth if i not in got]),
            "wrong_outcome": len([i for i in truth if i in got and got[i] != truth[i]]),
            "unknown_ids": len([i for i in got if i not in now_outcomes])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--arm", choices=("A", "B"), required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    key = os.environ["TCKG_API_KEY"]
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex_now, ex_t = _export(SPACE, now_iso, key), _export(SPACE, T, key)
    truth = oracle(ex_now, T)
    tckg_t = {r["target_id"]: r["outcome"] for r in ex_t.get("resolutions", []) if r["outcome"] in ("hit", "miss")}
    assert truth == tckg_t, f"oracle and TCKG as_of export disagree: {truth} vs {tckg_t}"
    now_outcomes = {r["target_id"]: r["outcome"] for r in ex_now["resolutions"]}
    text = PROMPT.format(what=WHAT[a.arm], t=T, rows=json.dumps(rows(ex_now, a.arm), ensure_ascii=False, indent=1))
    if a.dry_run:
        print(text[:3000])
        print(f"... {len(text)} chars; truth {truth}; decided after T: {len(now_outcomes) - len(truth)}")
        return 0
    out = {"kind": "time_ledger", "arm": a.arm, "model": a.model, "space": SPACE, "as_of": T, "exported_now": now_iso,
           "truth": truth, "decided_after_T": len(now_outcomes) - len(truth), "n_statements": len(rows(ex_now, "A")),
           "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(), "prompt_template": PROMPT,
           "oracle_matches_tckg_as_of_export": True, "runs": []}
    for i in range(a.runs):
        try:
            resp, wall = call_cli(a.model, text)
        except SystemExit as e:
            resp, wall = {"content": [{"type": "text", "text": f"error: {e}"}], "usage": None}, float("nan")
        raw = resp["content"][0]["text"]
        ans = parse(raw)
        s = score(ans, truth, now_outcomes)
        out["runs"].append({"run": i + 1, "wall_s": wall, "usage": resp.get("usage"),
                            "cost_usd": cost(a.model, resp["usage"]) if resp.get("usage") else None,
                            "cli_cost_usd": resp.get("cli_cost_usd"), "answer": ans, "score": s,
                            "raw_tail": raw[-1500:]})
        print(f"{a.arm} {a.model} run {i + 1}: {wall:.1f}s {({k: v for k, v in s.items() if k != 'leaked_ids'})}",
              flush=True)
    path = HERE / "results" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-time-{a.arm}-{a.model}.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
