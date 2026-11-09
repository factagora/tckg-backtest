"""run_backtest(request) -> response dict, io-v0 section 7. Pure apart from `now` (as_of_in_future only)."""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Callable

import jsonschema

from tckg_backtest.canonical import canonical, request_sha256

CONTRACT = "tckg-backtest-io/v0"
CONTRACT_REVISION = "v0.2"
ENGINE_VERSION = "0.1.0"
MOCK_VERSION = "0.0.0+mock"
CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contract" / "v0"


@cache
def _json(name: str):
    return json.loads((CONTRACT_DIR / name).read_text())


@cache
def _validator(name: str):
    return jsonschema.Draft202012Validator(_json(name))


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def response(status: str, rsha: str | None, *, errors=(), certificate=None, trades=(), summary=None,
             no_trade_reason=None, now: datetime | None = None) -> dict:
    return {"contract": CONTRACT, "status": status, "no_trade_reason": no_trade_reason, "request_sha256": rsha,
            "trades": list(trades), "summary": summary, "certificate": certificate, "errors": list(errors),
            "computed_at": _iso(now or datetime.now(UTC))}


def validate_request(request) -> list[dict]:
    """Errors (io-v0 6.2), [] when valid. All schema violations are listed, not just the first."""
    if not isinstance(request, dict):
        return [{"code": "invalid_request", "message": "The request body must be a JSON object."}]
    if "contract" in request and request["contract"] != CONTRACT:
        return [{"code": "contract_mismatch", "field": "contract",
                 "message": f"contract must be {CONTRACT!r}, got {request['contract']!r}."}]
    errors = [{"code": "invalid_request", "message": e.message,
               **({"field": ".".join(map(str, e.absolute_path))} if e.absolute_path else {})}
              for e in sorted(_validator("request.schema.json").iter_errors(request), key=lambda e: list(map(str, e.absolute_path)))]
    try:
        canonical(request)
    except (ValueError, TypeError) as e:
        errors.append({"code": "invalid_request", "message": str(e)})
    return errors


@cache
def _goldens() -> dict[str, dict]:
    """request_sha256 -> golden response, from contract/v0/response-*.json."""
    return {(r := _json(p.name))["request_sha256"]: r for p in sorted(CONTRACT_DIR.glob("response-*.json"))}


def mock_backtest(request: dict, *, now: datetime | None = None) -> dict:
    """The golden response for this request (io-v0 6.7 service_mock), or mock_no_golden."""
    if errors := validate_request(request):
        return response("invalid", None, errors=errors, now=now)
    rsha = request_sha256(request)
    if (golden := _goldens().get(rsha)) is None:
        return response("error", rsha, now=now, errors=[{
            "code": "mock_no_golden", "message": "The service runs in mock mode and no golden matches this request.",
            "detail": {"mock_reason": "service_mock"}}])
    out = copy.deepcopy(golden)
    out["certificate"].update(mock=True, mock_reason="service_mock", engine_version=MOCK_VERSION)
    out["computed_at"] = _iso(now or datetime.now(UTC))
    return out


def run_backtest(request: dict, *, load_prices: Callable[[str], bytes] | None = None,
                 now: datetime | None = None) -> dict:
    # Stage A: goldens only; the engine replaces this in Stage B.
    return mock_backtest(request, now=now)
