"""HTTP for run_backtest (io-v0 section 7). The Bearer token is checked here; Cloud Run IAM is left open."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from tckg_backtest import demo
from tckg_backtest.claims import run_claims_backtest

from tckg_backtest.core import CONTRACT, CONTRACT_REVISION, ENGINE_VERSION, MOCK_VERSION, mock_backtest, response
from tckg_backtest.engine import run_backtest

MAX_BODY = 1_000_000   # io-v0 7: 1 MB of inline bars
MAX_BODY_V1 = 16_000_000  # v1: years of daily bars for dozens of symbols
ERROR_HTTP = {"unauthorized": 401, "prices_unavailable": 503}

app = FastAPI(title="tckg-backtest", version=ENGINE_VERSION)
CONSOLE = Path(__file__).resolve().parent / "console.html"


def _engine() -> str:
    """TCKG_BACKTEST_ENGINE=mock serves the goldens (io-v0 6.7 service_mock); anything else runs the engine."""
    return "mock" if os.environ.get("TCKG_BACKTEST_ENGINE") == "mock" else "engine"


def _reply(body: dict) -> JSONResponse:
    status = body["status"]
    code = 200 if status in ("ok", "no_trade", "refused") else 400 if status == "invalid" else 500
    if body["errors"]:
        code = ERROR_HTTP.get(body["errors"][0]["code"], code)
    version = (body["certificate"] or {}).get("engine_version") or (MOCK_VERSION if _engine() == "mock" else ENGINE_VERSION)
    return JSONResponse(body, status_code=code,
                        headers={"X-Tckg-Backtest-Version": version, "X-Tckg-Backtest-Contract": "v0"})


def _invalid(code: str, message: str) -> JSONResponse:
    return _reply(response("invalid", None, errors=[{"code": code, "message": message}]))


@app.get("/v0/health")
def health():
    return {"status": "ok", "contract": CONTRACT, "contract_revision": CONTRACT_REVISION,
            "engine_version": ENGINE_VERSION, "engine": _engine()}


def _authorized(request: Request) -> bool:
    token = os.environ.get("TCKG_BACKTEST_TOKEN", "")
    given = request.headers.get("authorization", "")
    # No configured token means nobody gets in (fail closed).
    return bool(token) and hmac.compare_digest(given.encode(), f"Bearer {token}".encode())


async def _json_body(request: Request, limit: int):
    """(body, None) or (None, error reply)."""
    raw = await request.body()
    if len(raw) > limit:
        return None, _invalid("invalid_request", f"The request body is over {limit} bytes.")
    try:
        return json.loads(raw), None
    except ValueError:
        return None, _invalid("invalid_request", "The request body is not JSON.")


@app.post("/v0/backtest")
async def backtest(request: Request):
    if not _authorized(request):
        return _invalid("unauthorized", "Missing or wrong bearer token.")
    body, err = await _json_body(request, MAX_BODY)
    if err:
        return err
    return _reply(mock_backtest(body) if _engine() == "mock" else run_backtest(body))


@app.post("/v1/backtest")
async def backtest_v1(request: Request):
    """Contract v1 (contract/v1): many FactBlocks with a call each x one claims Playbook, unit-bet portfolio."""
    if not _authorized(request):
        return _invalid("unauthorized", "Missing or wrong bearer token.")
    body, err = await _json_body(request, MAX_BODY_V1)
    if err:
        return err
    out = run_claims_backtest(body)
    code = {"ok": 200, "no_trade": 200, "refused": 200, "invalid": 400}.get(out["status"], 500)
    return JSONResponse(out, status_code=code,
                        headers={"X-Tckg-Backtest-Version": ENGINE_VERSION, "X-Tckg-Backtest-Contract": "v1"})


@app.get("/", response_class=HTMLResponse)
def console():
    return CONSOLE.read_text()


@app.get("/v0/demo")
def demo_info():
    return demo.info()


@app.post("/v0/demo/run")
async def demo_run(request: Request):
    """No token: only the bundled requests run, so the client picks a case and an as_of, nothing else."""
    raw = await request.body()
    try:
        body = json.loads(raw) if len(raw) <= 1000 else None
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("case") not in demo.CASES or set(body) - {"case", "as_of"} \
            or not isinstance(body.get("as_of", ""), (str, type(None))):
        return _invalid("invalid_request", f"Body is {{case: one of {sorted(demo.CASES)}, as_of?: ISO UTC}}.")
    out = demo.run(body["case"], body.get("as_of"))
    return JSONResponse(out, headers={"X-Tckg-Backtest-Version": ENGINE_VERSION})
