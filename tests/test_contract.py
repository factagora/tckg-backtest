import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from tckg_backtest import ENGINE_VERSION, request_sha256, run_backtest, validate_request
from tckg_backtest.core import CONTRACT_DIR, mock_backtest
from tckg_backtest.server import app

NAMES = ["01-known-open", "02-hindsight-said", "03-candidate-playbook", "04-error-trigger-after-as-of"]
NOW = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)
TOKEN = "test-token"


def load(name):
    return json.loads((CONTRACT_DIR / name).read_text())


def schema(name):
    return jsonschema.Draft202012Validator(load(name))


def as_mock(golden):
    g = json.loads(json.dumps(golden))
    g["certificate"].update(mock=True, mock_reason="service_mock", engine_version="0.0.0+mock")
    return g


def strip(r):
    return {k: v for k, v in r.items() if k != "computed_at"}


@pytest.mark.parametrize("name", NAMES)
def test_golden_pair_is_schema_valid_and_hash_matches(name):
    req, resp = load(f"request-{name}.json"), load(f"response-{name}.json")
    schema("request.schema.json").validate(req)
    schema("response.schema.json").validate(resp)
    assert validate_request(req) == []
    assert request_sha256(req) == resp["request_sha256"] == resp["certificate"]["request_sha256"]


@pytest.mark.parametrize("name", NAMES)
def test_mock_serves_golden(name):
    got = mock_backtest(load(f"request-{name}.json"), now=NOW)
    schema("response.schema.json").validate(got)
    assert strip(got) == strip(as_mock(load(f"response-{name}.json")))


def test_mock_no_golden_and_invalid():
    req = load("request-01-known-open.json")
    req["as_of"] = "2026-11-07T00:00:00Z"
    got = mock_backtest(req, now=NOW)
    schema("response.schema.json").validate(got)
    assert got["status"] == "error" and got["errors"][0]["code"] == "mock_no_golden"
    for bad, code in [({**req, "extra": 1}, "invalid_request"), ({**req, "contract": "x"}, "contract_mismatch"),
                      ({**req, "as_of": "2026-11-08T09:00:00+09:00"}, "invalid_request")]:
        got = mock_backtest(bad, now=NOW)
        schema("response.schema.json").validate(got)
        assert got["status"] == "invalid" and got["errors"][0]["code"] == code and got["request_sha256"] is None


def test_canonical_refuses_exponent_numbers():
    req = load("request-01-known-open.json")
    req["prices"]["inline"]["bars"]["000660"][0]["open"] = 1e-5
    assert any(e["code"] == "invalid_request" for e in validate_request(req))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TCKG_BACKTEST_TOKEN", TOKEN)
    return TestClient(app)


def test_health_is_open(client):
    r = client.get("/v0/health")
    assert r.status_code == 200
    assert r.json()["contract"] == "tckg-backtest-io/v0" and r.json()["contract_revision"] == "v0.3"
    assert r.json()["engine_version"]


@pytest.mark.parametrize("auth", [None, "Bearer wrong", TOKEN])
def test_backtest_needs_bearer(client, auth):
    headers = {"Authorization": auth} if auth else {}
    r = client.post("/v0/backtest", json=load("request-01-known-open.json"), headers=headers)
    assert r.status_code == 401
    schema("response.schema.json").validate(r.json())
    assert r.json()["errors"][0]["code"] == "unauthorized"


def test_backtest_unauthorized_when_server_has_no_token(monkeypatch):
    monkeypatch.delenv("TCKG_BACKTEST_TOKEN", raising=False)
    r = TestClient(app).post("/v0/backtest", json={}, headers={"Authorization": "Bearer "})
    assert r.status_code == 401


@pytest.mark.parametrize("name", NAMES)
def test_backtest_http(client, name):
    r = client.post("/v0/backtest", json=load(f"request-{name}.json"), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.headers["X-Tckg-Backtest-Contract"] == "v0"
    assert r.headers["X-Tckg-Backtest-Version"] == r.json()["certificate"]["engine_version"]
    schema("response.schema.json").validate(r.json())
    assert r.json()["request_sha256"] == load(f"response-{name}.json")["request_sha256"]


def test_backtest_http_invalid(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post("/v0/backtest", content=b"not json", headers=h)
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "invalid_request"
    r = client.post("/v0/backtest", json={"contract": "tckg-backtest-io/v1"}, headers=h)
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "contract_mismatch"
    r = client.post("/v0/backtest", json={"contract": "tckg-backtest-io/v0"}, headers=h)
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "invalid_request"
    schema("response.schema.json").validate(r.json())


def same(got, want, path="$"):
    """io-v0 6.6: strings and ints exactly, floats within 1e-12."""
    if isinstance(want, float) or isinstance(got, float):
        assert abs(got - want) <= 1e-12, path
    elif isinstance(want, dict):
        assert isinstance(got, dict) and set(got) == set(want), (path, set(got) ^ set(want))
        for k in want:
            same(got[k], want[k], f"{path}.{k}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), path
        for i, (g, w) in enumerate(zip(got, want)):
            same(g, w, f"{path}[{i}]")
    else:
        assert got == want, (path, got, want)


@pytest.mark.parametrize("name", NAMES)
def test_engine_recomputes_golden(name):
    got = run_backtest(load(f"request-{name}.json"), now=NOW)
    schema("response.schema.json").validate(got)
    want = load(f"response-{name}.json")
    assert got["certificate"]["mock"] is False and got["certificate"]["engine_version"] == ENGINE_VERSION
    got["certificate"]["engine_version"] = want["certificate"]["engine_version"]
    same(strip(got), strip(want))


def engine(req, now=NOW):
    got = run_backtest(req, now=now)
    schema("response.schema.json").validate(got)
    return got


def code(got):
    return got["errors"][0]["code"] if got["errors"] else got["no_trade_reason"]


def test_engine_edge_cases():
    r01, r02, r03 = (load(f"request-{n}.json") for n in NAMES[:3])
    masked_verdict = engine({**r03, "as_of": "2026-10-25T00:00:00Z"})
    assert masked_verdict["status"] == "no_trade" and code(masked_verdict) == "trigger_not_met"
    assert [m["input"] for m in masked_verdict["certificate"]["masked"]] == ["factblock.verdict", "prices.bars", "prices.bars"]
    assert code(engine({**r01, "as_of": "2026-10-20T20:00:00Z"})) == "entry_after_as_of"
    mid = engine({**r01, "as_of": "2026-10-21T03:00:00Z"})   # inside the entry session: close not known yet
    assert mid["status"] == "ok" and mid["trades"][0]["open"] and mid["trades"][0]["ret"] == 0
    assert {"input": "prices.close", "symbol": "000660", "from": "2026-10-21", "to": "2026-10-21", "count": 1} \
        in mid["certificate"]["masked"]
    assert code(engine({**r02, "playbook": r03["playbook"]})) == "unsupported_combination"
    fb = {**r01["factblock"], "assets": [{"symbol": "AAPL", "market": "XNAS"}]}
    assert code(engine({**r01, "factblock": fb})) == "market_not_on_calendar"
    bars = {**r03["prices"]["inline"]["bars"], "000660": r03["prices"]["inline"]["bars"]["000660"][1:]}
    got = engine({**r03, "prices": {"inline": {**r03["prices"]["inline"], "bars": bars}}})
    assert got["status"] == "refused" and got["errors"][0]["detail"] == {"symbol": "000660", "date": "2026-10-30", "side": "open"}
    assert code(engine({**r01, "as_of": "2026-11-10T00:00:00Z"})) == "as_of_in_future"
    late = {**r01["factblock"], "known_at": "2027-12-29T00:00:00Z"}
    assert code(engine({**r01, "factblock": late, "as_of": "2028-01-05T00:00:00Z"}, now=datetime(2028, 2, 1, tzinfo=UTC))) \
        == "calendar_out_of_range"
    assert code(engine({**r01, "calendar": {"id": "XKRX", "sha256": "0" * 64}})) == "invalid_request"
    assert engine({**r01, "calendar": {"id": "XKRX", "sha256": load("response-01-known-open.json")["certificate"]["calendar"]["sha256"]}})["status"] == "ok"
    ref = engine({**r01, "prices": {"ref": {"uri": "gs://b/p.csv", "format": "tckg-trader-prices-csv"}}})
    assert ref["status"] == "error" and code(ref) == "prices_unavailable"


def test_backtest_http_mock_mode(client, monkeypatch):
    monkeypatch.setenv("TCKG_BACKTEST_ENGINE", "mock")
    assert client.get("/v0/health").json()["engine"] == "mock"
    r = client.post("/v0/backtest", json=load("request-01-known-open.json"), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json()["certificate"]["mock_reason"] == "service_mock" and r.headers["X-Tckg-Backtest-Version"] == "0.0.0+mock"


@pytest.mark.parametrize("name", NAMES)
def test_tckg_source_rebuilds_golden_request(name):
    """The fixture FactBlock + fixture prices give byte-identical golden requests (io-v0 2.1 window)."""
    from tckg_backtest.sources import tckg
    want = load(f"request-{name}.json")
    got = tckg.request(json.loads(tckg.FACTBLOCK_FIXTURE.read_text()), want["playbook"], tckg.PRICES_FIXTURE,
                       mode=want["mode"], as_of=want["as_of"])
    assert request_sha256(got) == request_sha256(want)


def test_tckg_source_maps_export(monkeypatch):
    """fetch_factblock from export rows (synthetic, no network): known_at kept to the microsecond, verdict from the
    resolution's ledger write time, non-XKRX assets left out by name."""
    from tckg_backtest.sources import tckg
    node = {"id": "stmt:x", "kind": "commitment", "statement": "s", "asserted_at": "2026-08-18T15:00:00+00:00",
            "known_at": "2026-10-20T13:11:23.254773+00:00",
            "payload": {"speaker": "SK hynix", "asserted_at_precision": "day", "assets": ["krx:000660", "us:SKHY"]}}
    res = {"target_id": "stmt:x", "outcome": "hit", "known_at": "2026-10-29T00:36:00.138833+00:00"}
    monkeypatch.setattr(tckg, "_export", lambda *a: {"nodes": [node], "resolutions": [res]})
    monkeypatch.setattr(tckg, "_call", lambda *a, **k: {"items": [{"backfill_batch": None}]})
    fb = tckg.fetch_factblock("stmt:x", "sp", "2026-11-08T00:00:00Z", "k")
    assert fb.pop("_left_out") == ["us:SKHY"]
    assert fb["known_at"] == "2026-10-20T13:11:23.254773Z" and fb["verdict"] == {"value": "hit", "decided_at": "2026-10-29T00:36:00.138833Z"}
    got = run_backtest(tckg.request(fb, load("request-01-known-open.json")["playbook"], tckg.PRICES_FIXTURE), now=NOW)
    assert got["status"] == "ok" and got["trades"][0]["entry"]["date"] == "2026-10-21"


def test_demo_runs_bundled_requests_without_token(monkeypatch):
    monkeypatch.delenv("TCKG_BACKTEST_TOKEN", raising=False)
    c = TestClient(app)
    assert "tckg-backtest console" in c.get("/").text
    info = c.get("/v0/demo").json()
    assert set(info["cases"]) == {"01", "02", "03", "05", "06"} and info["cases"]["02"]["ai"]["runs"]
    one = c.post("/v0/demo/run", json={"case": "01"}).json()
    assert one["response"]["trades"] == load("response-01-known-open.json")["trades"]
    leak = c.post("/v0/demo/run", json={"case": "05"}).json()
    assert leak["response"]["trades"] == one["response"]["trades"]
    assert sum(m.get("count", 0) for m in leak["response"]["certificate"]["masked"]) == 20
    early = c.post("/v0/demo/run", json={"case": "01", "as_of": "2026-10-15T00:00:00Z"}).json()["response"]
    assert early["status"] == "refused" and early["errors"][0]["code"] == "trigger_after_as_of"
    hashes = {c.post("/v0/demo/run", json={"case": "03"}).json()["response_sha256"] for _ in range(3)}
    assert len(hashes) == 1
    # The client picks a case and as_of only; anything else is refused, and /v0/backtest still needs the token.
    many = c.post("/v0/demo/run", json={"case": "06"}).json()["response"]
    assert many["contract"] == "tckg-backtest-io/v1" and many["summary"]["n_trades"] == 8
    assert {"factblock_id": "fb-09", "reason": "not_known_at_as_of"} in many["skipped"]
    for bad in [{"case": "04"}, {"case": "01", "prices": {}}, {"case": "01", "as_of": 5}, []]:
        assert c.post("/v0/demo/run", json=bad).status_code == 400
    assert c.post("/v0/backtest", json=load("request-01-known-open.json")).status_code == 401
