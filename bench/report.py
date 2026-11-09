"""Combine the latest engine result and the latest AI result per model into bench/results/<ts>.json and
bench/RESULTS.md.

    python3 bench/report.py
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"


def latest() -> dict:
    """{'engine': result, 'ai:<model>': result}, newest file of each."""
    out = {}
    for p in sorted(RES.glob("*-engine.json")) + sorted(RES.glob("*-ai-*.json")):
        x = json.loads(p.read_text())
        out["engine" if x["kind"] == "engine" else f"ai:{x['model']}:{x.get('prompt_kind', 'full')}"] = x | {"file": p.name}
    return out


def row(system: str, name: str, s: dict | None, x: dict) -> str:
    if s is None:
        return f"| {name} | {system} | skipped (mock) | | | | | |"
    t = f"{s['median_s'] * 1e3:.3f} ms" if x["kind"] == "engine" else f"{s['median_s']:.1f} s"
    c = "0" if x["kind"] == "engine" else (f"${s['cost_usd_mean']:.4f}" if s["cost_usd_mean"] is not None else "n/a")
    n = s["n"]
    return (f"| {name} | {system} | {t} | {c} | {s['correct']}/{n} | {s['distinct']}/{n} ({s.get('distinct_material', '?')}/{n}) | {s['future']}/{n} "
            f"| {s['parse_fail']}/{n} |")


def main() -> int:
    got = latest()
    if not got:
        raise SystemExit("no results in bench/results")
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (RES / f"{ts}.json").write_text(json.dumps({"generated_at": ts, "sources": got}, indent=1) + "\n")
    names = next(iter(got.values()))["requests"].keys()
    lines = ["# Bench: tckg-backtest engine vs an LLM prompted with the same rules", "",
             f"Generated {ts} by `python3 bench/report.py` from {', '.join(x['file'] for x in got.values())}.", "",
             "| Request | System | Median time | Cost per run (est.) | Correct | Distinct answers | Future-info flags "
             "| Parse failures |", "|---|---|---|---|---|---|---|---|"]
    for name in names:
        for key, x in got.items():
            label = (f"engine ({'MOCK: ' if x['mock'] else ''}{x['engine']}, commit {x['commit']})"
                     if key == "engine" else f"{x['model']}, {x.get('prompt_kind', 'full')} prompt")
            lines.append(row(label, name, x["requests"][name]["summary"], x))
    ai = [x for k, x in got.items() if k != "engine"]
    lines += ["", "## Definitions", "",
              "- Correct: entry date and exit date equal the golden response, and |excess - golden| <= 0.0005.",
              "- Distinct answers: number of different parsed answers across the runs, all fields exact; in brackets, "
              "counting only dates, open flag and excess rounded to 4 decimals (most exact differences are float "
              "digits).",
              "- AI runs went through `claude -p` (bench/ai_prompt.py --via cli): Claude Code login, tools off, system "
              "prompt replaced by one line, from an empty directory. Time includes CLI start (about 1.5 s); "
              "`duration_api_ms` per run is in the result file. Cost column = returned tokens x list price; the "
              "CLI's own total_cost_usd (with cache pricing) is also in the file.",
              "- full prompt: the contract rules in plain English. short prompt: only 'backtest this as of as_of, do "
              "not use information after as_of' plus the answer format. Both get the same request JSON and XKRX "
              "calendar.",
              "- Future-info flags: runs whose answer enters at or before the trigger time, prices or marks a session "
              "after as_of, or uses a price from a bar after as_of.",
              "- 05-leak-after-as-of: request 01 plus synthetic bars for 2026-11-09..20 (after as_of). The correct "
              "answer ignores them, so the golden is response 01.",
              "- Time: engine is in-process `run_backtest` wall time (no HTTP, no import). AI is one Messages API "
              "call, wall time from this container.",
              "- Cost: from returned usage x assumed list price "
              + "; ".join(f"{x['model']} ${x['price_per_mtok_usd_assumed'][0]}/${x['price_per_mtok_usd_assumed'][1]} "
                          "per MTok in/out" for x in ai if x.get("price_per_mtok_usd_assumed")) + ".",
              "- Goldens: bench/v0, byte copy of factagora/tckg-trader docs/tckg-backtest/v0 at fb3e6c2.", "",
              "## Reproduce", "", "```", "python3 bench/engine.py --runs 5",
              "ANTHROPIC_API_KEY=... python3 bench/ai_prompt.py --model claude-sonnet-5-5 --runs 5",
              "python3 bench/report.py", "```", "",
              "The full prompt is in each AI result file (`requests.<name>.prompt`), or "
              "`python3 bench/ai_prompt.py --dry-run --only <name>`."]
    (HERE / "RESULTS.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:lines.index("## Definitions")]))
    if not any(k.startswith("ai:") for k in got):
        print("WARNING: no AI result in bench/results; run bench/ai_prompt.py first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
