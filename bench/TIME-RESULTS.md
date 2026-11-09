# Time experiment: results

Pre-registration: `bench/TIME-PREREG.md` (committed before the runs). Script: `bench/time_ledger.py`. Raw: `bench/results/*-time-*.json`.

Question: as of 2026-10-27, which statements in TCKG space `theme:kr-memory-semis` (67 real statements, 21 verdicts today) had a verdict? Truth: 1 (Meta, miss); 20 verdicts came after T. Truth is an oracle over the ledger rows, equal to TCKG's own export at as_of=T.

| Arm (what the model gets) | Model | Exact match | Verdicts from after T counted (per run) | Median time | Cost per run (est.) |
|---|---|---|---|---|---|
| A: TCKG rows (known_at, verdict recorded_at) | claude-sonnet-5-5 | 5/5 | 0, 0, 0, 0, 0 | 6.5 s | $0.038 |
| A: TCKG rows | claude-opus-5-5 | 5/5 | 0, 0, 0, 0, 0 | 8.0 s | $0.078 |
| B: sheet rows (said_at, today's outcome) | claude-sonnet-5-5 | 0/5 | 1, 1, 1, 1, 1 | 19.2 s | $0.049 |
| B: sheet rows | claude-opus-5-5 | 0/5 | 11, 2, 2, 12, 1 | 30.4 s | $0.111 |
| TCKG export at as_of=T | (no model) | exact by construction of the read | 0 | one HTTP read | 0 |

Notes:
- Matches the pre-registered expectation for A. For B the models did better than "many leaked" in most runs: they guessed release dates from the usual earnings calendar and dropped most later verdicts. They still never matched the truth.
- The verdict leaked in every B run is Samsung's 10-08 preliminary operating profit claim (hit). The models treated a company's own preliminary number as already confirmed; the ledger recorded the verdict on 11-05, after the full results. A reader could argue it either way; without it Sonnet B would be 5/5 and Opus B 0/5.
- Opus B runs with 11-12 leaks counted SK hynix, Microsoft and other verdicts whose events happened after T.
- Models via `claude -p`, tools off, 5 runs per cell. Cost = tokens x list price (Sonnet $2/$10, Opus $4/$20 per MTok).
- The 2026-10-27 rows are real ledger rows; nothing was synthesized. The ledger data is not in this repo (TCKG is private).
