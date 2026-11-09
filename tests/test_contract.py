import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from tckg_backtest import request_sha256, validate_request
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
    assert r.json()["contract"] == "tckg-backtest-io/v0" and r.json()["contract_revision"] == "v0.2"
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
