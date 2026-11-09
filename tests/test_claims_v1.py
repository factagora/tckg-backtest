"""Contract v1 (claims Playbook, unit-bet portfolio): parity with factagora.ai claims_v1 and the point-in-time rules."""
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tckg_backtest.claims import V1_DIR, run_claims_backtest
from tckg_backtest.server import app

NOW = datetime(2026, 11, 9, tzinfo=UTC)
PARITY = json.loads((Path(__file__).parent / "fixtures" / "claims_v1_parity.json").read_text())["scenarios"]


def example():
    return json.loads((V1_DIR / "request-01-claims-as-known.json").read_text())


def to_request(sc: dict) -> dict:
    """A claims_v1 scenario (BundleClaim[], PriceBook, rules, asOf date) as a v1 hindsight_said_at request."""
    r = sc["rules"]
    said = lambda c: c["assertedAt"].replace(".000Z", "Z")
    return {"contract": "tckg-backtest-io/v1", "mode": "hindsight_said_at", "as_of": sc["asOf"] + "T23:59:59Z",
            "factblocks": [{"id": c["id"], "speaker": {"id": c["speakerId"]}, "said_at": said(c), "known_at": said(c),
                            "backfilled": False, "call": {"symbol": c["symbol"], "market": c["market"],
                                                          "direction": c["direction"], "horizon_days": c["horizonDays"]}}
                           for c in sc["claims"]],
            "playbook": {"id": "claims-follow", "version": r["version"], "entry": "next_session_open",
                         "exit": "close_on_or_after_horizon", "default_horizon_days": r["defaultHorizonDays"],
                         "max_horizon_days": r["maxHorizonDays"], "cost_bps": r["costBps"],
                         "exit_on_opposite_call": r["exitOnOppositeClaim"], "benchmark": {"symbol": r["benchmark"]},
                         "benchmark_matches_direction": r["benchmarkMatchesDirection"],
                         "short_loss_cap": r["shortLossCap"]},
            "portfolio": {"initial_capital": 100000, "unit_fraction": 0.05},
            "prices": {"inline": {"currency": "USD", "source": "synthetic", "bars": sc["book"]}}}


@pytest.mark.parametrize("k", range(len(PARITY)))
def test_hindsight_mode_gives_the_same_trades_as_claims_v1(k):
    sc = PARITY[k]
    got = {t["factblock_id"]: t for t in run_claims_backtest(to_request(sc), now=NOW)["trades"]}
    assert set(got) == {t["claim_id"] for t in sc["trades"]}
    for w in sc["trades"]:
        g = got[w["claim_id"]]
        assert (g["entry"]["date"], g["entry"]["price"]) == (w["entry_date"], w["entry_px"])
        assert ((g["exit"] or {}).get("date"), (g["exit"] or {}).get("price")) == (w["exit_date"], w["exit_px"])
        assert g["exit_reason"].replace("opposite_call", "opposite_claim") == w["exit_reason"]
        assert (g["closed_by"], g["horizon_days"], g["hit"]) == (w["closed_by_claim_id"], w["horizon_days"], w["hit"])
        for a, b in [(g["ret"], w["ret"]), (g["benchmark_raw_ret"], w["spy_ret"]),
                     (g["benchmark_ret"], w["benchmark_ret"]), (g["excess"], w["excess"])]:
            assert a == pytest.approx(b, abs=1e-12) if b is not None else a is None


def test_example_golden_and_determinism():
    want = json.loads((V1_DIR / "response-01-claims-as-known.json").read_text())
    for _ in range(3):
        got = run_claims_backtest(example(), now=NOW)
        assert {**got, "computed_at": None} == {**want, "computed_at": None}


def test_as_known_waits_for_the_ledger_and_masks_what_came_later():
    r = run_claims_backtest(example(), now=NOW)
    t = {x["factblock_id"]: x for x in r["trades"]}
    # said 01-20, in the ledger 03-02: as_known enters after known_at, hindsight after said_at
    assert t["fb-4-backfilled"]["entry"]["date"] == "2026-03-03"
    assert {"factblock_id": "fb-5-known-after-as-of", "reason": "not_known_at_as_of"} in r["skipped"]
    c = r["certificate"]
    assert c["masked"][0] == {"input": "factblocks", "field": "known_at", "count": 1, "ids": ["fb-5-known-after-as-of"]}
    assert all(m["from"] >= "2026-04-01" for m in c["masked"][1:])  # no bar after as_of is used
    assert c["used_before_known"] == [] and c["hindsight"] is False and c["backfilled"] is True
    assert c["reproducible"] is False and c["not_reproducible_because"] == ["backfilled_known_at"]
    h = run_claims_backtest({**example(), "mode": "hindsight_said_at"}, now=NOW)
    th = {x["factblock_id"]: x for x in h["trades"]}
    assert th["fb-4-backfilled"]["entry"]["date"] == "2026-01-21"
    assert [u["factblock_id"] for u in h["certificate"]["used_before_known"]] == ["fb-4-backfilled", "fb-5-known-after-as-of"]


def test_prices_after_as_of_never_change_the_answer():
    base = run_claims_backtest(example(), now=NOW)
    req = example()
    for bars in req["prices"]["inline"]["bars"].values():
        for b in bars:
            if b["date"] >= "2026-04-01":
                b["open"], b["close"] = b["open"] * 3, b["close"] * 3
    got = run_claims_backtest(req, now=NOW)
    assert got["trades"] == base["trades"] and got["summary"] == base["summary"]


def test_portfolio_is_unit_bets_in_exit_order():
    r = run_claims_backtest(example(), now=NOW)
    s, done = r["summary"], [t for t in r["trades"] if t["ret"] is not None]
    assert s["n_resolved"] == len(done) == 3 and s["n_pending"] == 1
    assert s["total_return"] == pytest.approx(sum(5000 * t["ret"] for t in done) / 100000)
    assert [p["date"] for p in s["curve"]] == sorted(p["date"] for p in s["curve"])


def test_refusals_and_invalid():
    future = {**example(), "as_of": "2027-01-01T00:00:00Z"}
    assert run_claims_backtest(future, now=NOW)["errors"][0]["code"] == "as_of_in_future"
    bad = example()
    bad["prices"]["inline"]["bars"]["AAA"].reverse()
    assert run_claims_backtest(bad, now=NOW)["status"] == "invalid"
    dup = example()
    dup["factblocks"].append(copy.deepcopy(dup["factblocks"][0]))
    assert run_claims_backtest(dup, now=NOW)["status"] == "invalid"
    assert run_claims_backtest({**example(), "contract": "tckg-backtest-io/v0"}, now=NOW)["errors"][0]["code"] == "contract_mismatch"
    nob = example()
    del nob["prices"]["inline"]["bars"]["SPY"]
    assert run_claims_backtest(nob, now=NOW)["status"] == "invalid"


def test_v1_endpoint_needs_the_token(monkeypatch):
    monkeypatch.setenv("TCKG_BACKTEST_TOKEN", "t")
    c = TestClient(app)
    assert c.post("/v1/backtest", json=example()).status_code == 401
    r = c.post("/v1/backtest", json=example(), headers={"Authorization": "Bearer t"})
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.headers["X-Tckg-Backtest-Contract"] == "v1"
