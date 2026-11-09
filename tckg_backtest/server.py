"""HTTP for run_backtest (io-v0 section 7). The Bearer token is checked here; Cloud Run IAM is left open."""
from __future__ import annotations

import hmac
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tckg_backtest.core import (CONTRACT, CONTRACT_REVISION, ENGINE_VERSION, MOCK_VERSION, mock_backtest, response,
                                run_backtest)

MAX_BODY = 1_000_000   # io-v0 7: 1 MB of inline bars
ERROR_HTTP = {"unauthorized": 401, "prices_unavailable": 503}

app = FastAPI(title="tckg-backtest", version=ENGINE_VERSION)


def _engine() -> str:
    """TCKG_BACKTEST_ENGINE=mock serves the goldens; engine runs the engine. Stage A: only mock exists."""
    return "mock" if os.environ.get("TCKG_BACKTEST_ENGINE", "mock") == "mock" else "engine"


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
