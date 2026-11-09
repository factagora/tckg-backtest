# Bench: tckg-backtest engine vs an LLM prompted with the same rules

Generated 20261109T011030Z by `python3 bench/report.py` from 20261109T011029Z-engine.json.

| Request | System | Median time | Cost per run (est.) | Correct | Distinct answers | Future-info flags | Parse failures |
|---|---|---|---|---|---|---|---|
| 01-known-open | engine (MOCK: mock (No module named 'tckg_backtest'), commit 4a1cc2c) | 0.086 ms | 0 | 5/5 | 1/5 | 0/5 | 0/5 |
| 02-hindsight-said | engine (MOCK: mock (No module named 'tckg_backtest'), commit 4a1cc2c) | 0.100 ms | 0 | 5/5 | 1/5 | 0/5 | 0/5 |
| 03-candidate-playbook | engine (MOCK: mock (No module named 'tckg_backtest'), commit 4a1cc2c) | 0.071 ms | 0 | 5/5 | 1/5 | 0/5 | 0/5 |

## Definitions

- Correct: entry date and exit date equal the golden response, and |excess - golden| <= 0.0005.
- Distinct answers: number of different parsed answers (all fields) across the runs.
- Future-info flags: runs whose answer enters at or before the trigger time, or prices/marks a session after as_of.
- Time: engine is in-process `run_backtest` wall time (no HTTP, no import). AI is one Messages API call, wall time from this container.
- Cost: from returned usage x assumed list price .
- Goldens: bench/v0, byte copy of factagora/tckg-trader docs/tckg-backtest/v0 at fb3e6c2.

## Reproduce

```
python3 bench/engine.py --runs 5
ANTHROPIC_API_KEY=... python3 bench/ai_prompt.py --model claude-sonnet-5-5 --runs 5
python3 bench/report.py
```

The full prompt is in each AI result file (`requests.<name>.prompt`), or `python3 bench/ai_prompt.py --dry-run --only <name>`.
