"""HTTP for run_backtest (io-v0 section 7). The Bearer token is checked here; Cloud Run IAM is left open."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from tckg_backtest import demo

from tckg_backtest.core import CONTRACT, CONTRACT_REVISION, ENGINE_VERSION, MOCK_VERSION, mock_backtest, response
from tckg_backtest.engine import run_backtest

MAX_BODY = 1_000_000   # io-v0 7: 1 MB of inline bars
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


@app.post("/v0/backtest")
async def backtest(request: Request):
    token = os.environ.get("TCKG_BACKTEST_TOKEN", "")
    given = request.headers.get("authorization", "")
    # No configured token means nobody gets in (fail closed).
    if not token or not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
        return _invalid("unauthorized", "Missing or wrong bearer token.")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _invalid("invalid_request", f"The request body is over {MAX_BODY} bytes.")
    try:
        body = json.loads(raw)
    except ValueError:
        return _invalid("invalid_request", "The request body is not JSON.")
    return _reply(mock_backtest(body) if _engine() == "mock" else run_backtest(body))


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
