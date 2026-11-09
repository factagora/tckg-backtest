# Time experiment: pre-registration

Written before any model run (2026-11-09). Script: `bench/time_ledger.py`.

- Data: hosted TCKG space `theme:kr-memory-semis`, predictions and claims (67) with verdict resolutions (21), exported as known now. Real ledger rows, nothing synthetic.
- Question to the model: as of 2026-10-27T00:00:00Z, which statements had a verdict, and what was it?
- Truth: 1 statement (Meta Q3 revenue guidance, miss, recorded 2026-10-22). The other 20 verdicts were recorded after T, and the events behind them (SK hynix 10-28, Microsoft, Samsung Q3 results) also happened after T, so "the ledger was late" does not explain them away.
- Truth comes from an oracle over the now-export (known_at <= T, verdict recorded_at <= T) with no engine code, and it equals TCKG's own export at as_of=T (asserted in the script).
- Arm A: rows carry known_at and verdict.recorded_at (what TCKG keeps). Arm B: rows carry said_at and today's outcome only (an overwrite-style sheet).
- Models: claude-sonnet-5-5 and claude-opus-5-5 via `claude -p`, tools off, 5 runs per arm.
- Score per run: exact match; leaked = verdicts listed that were recorded only after T; missed; wrong outcome.

Expected before running:
- A: the models filter by recorded_at and match the truth in most runs (4-5 of 5). If they do, the finding is "with TCKG's timestamps, an LLM can filter too"; the timestamps are the asset.
- B: the models cannot know when each verdict became known. Expect many leaked verdicts in most runs, unless a model infers event dates from the statements (it may drop some; we report whatever it does).
