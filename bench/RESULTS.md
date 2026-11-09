# Bench: tckg-backtest engine vs an LLM prompted with the same rules

Generated 20261109T013901Z by `python3 bench/report.py` from 20261109T013901Z-engine.json, 20261109T013316Z-ai-claude-sonnet-5-5-full.json, 20261109T013857Z-ai-claude-sonnet-5-5-short.json.

| Request | System | Median time | Cost per run (est.) | Correct | Distinct answers | Future-info flags | Parse failures |
|---|---|---|---|---|---|---|---|
| 01-known-open | engine (MOCK: mock (No module named 'tckg_backtest'), commit e06dea7) | 0.061 ms | 0 | 5/5 | 1/5 (1/5) | 0/5 | 0/5 |
| 01-known-open | claude-sonnet-5-5, full prompt | 14.1 s | $0.0238 | 5/5 | 3/5 (1/5) | 0/5 | 0/5 |
| 01-known-open | claude-sonnet-5-5, short prompt | 11.2 s | $0.0186 | 5/5 | 2/5 (1/5) | 0/5 | 0/5 |
| 02-hindsight-said | engine (MOCK: mock (No module named 'tckg_backtest'), commit e06dea7) | 0.072 ms | 0 | 5/5 | 1/5 (1/5) | 0/5 | 0/5 |
| 02-hindsight-said | claude-sonnet-5-5, full prompt | 13.2 s | $0.0246 | 4/5 | 5/5 (2/5) | 0/5 | 0/5 |
| 02-hindsight-said | claude-sonnet-5-5, short prompt | 12.6 s | $0.0203 | 1/5 | 5/5 (3/5) | 0/5 | 0/5 |
| 03-candidate-playbook | engine (MOCK: mock (No module named 'tckg_backtest'), commit e06dea7) | 0.047 ms | 0 | 5/5 | 1/5 (1/5) | 0/5 | 0/5 |
| 03-candidate-playbook | claude-sonnet-5-5, full prompt | 14.0 s | $0.0227 | 5/5 | 2/5 (1/5) | 0/5 | 0/5 |
| 03-candidate-playbook | claude-sonnet-5-5, short prompt | 12.1 s | $0.0185 | 5/5 | 4/5 (1/5) | 0/5 | 0/5 |
| 05-leak-after-as-of | engine (MOCK: mock (No module named 'tckg_backtest'), commit e06dea7) | skipped (mock) | | | | | |
| 05-leak-after-as-of | claude-sonnet-5-5, full prompt | 13.5 s | $0.0262 | 5/5 | 4/5 (1/5) | 0/5 | 0/5 |
| 05-leak-after-as-of | claude-sonnet-5-5, short prompt | 11.3 s | $0.0211 | 5/5 | 3/5 (1/5) | 0/5 | 0/5 |

## Definitions

- Correct: entry date and exit date equal the golden response, and |excess - golden| <= 0.0005.
- Distinct answers: number of different parsed answers across the runs, all fields exact; in brackets, counting only dates, open flag and excess rounded to 4 decimals (most exact differences are float digits).
- AI runs went through `claude -p` (bench/ai_prompt.py --via cli): Claude Code login, tools off, system prompt replaced by one line, from an empty directory. Time includes CLI start (about 1.5 s); `duration_api_ms` per run is in the result file. Cost column = returned tokens x list price; the CLI's own total_cost_usd (with cache pricing) is also in the file.
- full prompt: the contract rules in plain English. short prompt: only 'backtest this as of as_of, do not use information after as_of' plus the answer format. Both get the same request JSON and XKRX calendar.
- Future-info flags: runs whose answer enters at or before the trigger time, prices or marks a session after as_of, or uses a price from a bar after as_of.
- 05-leak-after-as-of: request 01 plus synthetic bars for 2026-11-09..20 (after as_of). The correct answer ignores them, so the golden is response 01.
- Time: engine is in-process `run_backtest` wall time (no HTTP, no import). AI is one Messages API call, wall time from this container.
- Cost: from returned usage x assumed list price claude-sonnet-5-5 $2.0/$10.0 per MTok in/out; claude-sonnet-5-5 $2.0/$10.0 per MTok in/out.
- Goldens: bench/v0, byte copy of factagora/tckg-trader docs/tckg-backtest/v0 at fb3e6c2.

## Reproduce

```
python3 bench/engine.py --runs 5
ANTHROPIC_API_KEY=... python3 bench/ai_prompt.py --model claude-sonnet-5-5 --runs 5
python3 bench/report.py
```

The full prompt is in each AI result file (`requests.<name>.prompt`), or `python3 bench/ai_prompt.py --dry-run --only <name>`.
