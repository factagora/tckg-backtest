"""Canonical JSON and hashes, io-v0 section 6.4. Values Python and JS would write differently are refused (ValueError)
before hashing, so a hash here equals factagora.ai's canonicalJson hash of the same request."""
import hashlib
import json
import math


def _js_safe(x, path: str = "$"):
    """x with integer-valued floats as ints (JS writes 1.0 and -0.0 as 1 and 0); ValueError on NaN/inf, |x| >= 1e21,
    a non-integer 0 < |x| < 1e-4 (exponent notation) and a key outside printable ASCII."""
    if isinstance(x, dict):
        for k in x:
            if not isinstance(k, str) or not all(" " <= c <= "~" for c in k):
                raise ValueError(f"{path}: key {k!r} is not printable ASCII (io-v0 6.4)")
        return {k: _js_safe(v, f"{path}.{k}") for k, v in x.items()}
    if isinstance(x, list):
        return [_js_safe(v, f"{path}[{i}]") for i, v in enumerate(x)]
    if isinstance(x, float):
        if not math.isfinite(x):
            raise ValueError(f"{path}: {x} is not a JSON number")
        if x.is_integer():
            x = int(x)
        elif abs(x) < 1e-4:
            raise ValueError(f"{path}: {x!r} is written in exponent notation (io-v0 6.4)")
    if isinstance(x, int) and not isinstance(x, bool) and abs(x) >= 10**21:
        raise ValueError(f"{path}: {x} is >= 1e21, which JS writes in exponent notation (io-v0 6.4)")
    return x


def canonical(x) -> bytes:
    return json.dumps(_js_safe(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def sha256(x) -> str:
    return hashlib.sha256(canonical(x)).hexdigest()


def request_sha256(request: dict) -> str:
    return sha256(request)
