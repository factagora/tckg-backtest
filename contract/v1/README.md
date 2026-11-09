# Contract v1: claims Playbook, unit-bet portfolio

`POST /v1/backtest` (Bearer, same token as v0). Request: `request.schema.json`. Example pair:
`request-01-claims-as-known.json` and `response-01-claims-as-known.json` (synthetic bars, not market data).

v0 is one FactBlock x one Playbook rule. v1 is many FactBlocks, each carrying an investable call (symbol, market,
direction, horizon), x one claims Playbook, with a unit-bet portfolio. The trade rules are the ones factagora.ai
`/backtests` (strategy claims_v1, rules v1 and v1.1) used; in `hindsight_said_at` mode v1 gives the same trades
(`tests/test_claims_v1.py` checks this against scenarios run through the claims_v1 code).

## Rules

1. Trigger: `known_at` in `as_known` mode (when the ledger had the call), `said_at` in `hindsight_said_at` mode.
   A FactBlock whose trigger is after `as_of` does not trade and is listed in `certificate.masked`.
2. Sessions are the dates of the bars given. A bar is usable when its close is known by `as_of`
   (16:00 New York for `US`, the end of the UTC day for `CRYPTO`); later bars are masked.
3. Entry: the open of the first bar whose date is after the trigger's exchange date (New York date for `US`, UTC
   date for `CRYPTO`).
4. One call per speaker, symbol and said day: a repeat in the same direction is skipped; opposite calls on the same
   day cancel and neither trades.
5. Horizon: the stated `horizon_days` capped at `max_horizon_days`, else `default_horizon_days`. Exit at the close
   of the first bar on or after entry date + horizon days. If `exit_on_opposite_call`, a later opposite call by the
   same speaker on the same symbol closes the position at that call's entry bar when that comes first.
   No exit bar by `as_of`: the trade is pending and `mark` is the last usable close.
6. ret = direction x (exit close / entry open - 1), floored at `-short_loss_cap` when set, minus 2 x `cost_bps`.
   Benchmark: open of the first benchmark bar on or after entry to the close of the last one on or before exit
   (`benchmark_raw_ret`); `benchmark_ret` takes the call's direction when `benchmark_matches_direction`.
   excess = ret - benchmark_ret. hit = 1 when the price moved the called way (before costs).
7. Portfolio: one unit = `initial_capital x unit_fraction` per trade, no compounding. The curve adds
   unit x ret for resolved trades in exit order; pending trades are not marked into it. `max_drawdown` is on that curve.

## Certificate

As in v0, plus `factblocks_sha256`. `hindsight` is true in `hindsight_said_at` mode; `used_before_known` lists
trades entered at an open before their `known_at`. `backfilled` is true when any traded FactBlock is marked
backfilled. `reproducible` is false when either holds.

## Not in v1 yet

Later: statistics on top of the trades (bootstrap intervals, Brier, causal-edge scores, premises as of entry) stay in
factagora.ai and are computed from the returned trades.
