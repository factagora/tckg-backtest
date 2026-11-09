"""Canonical JSON and hashes, io-v0 section 6.4. Numbers are written as JS writes them and values the two cannot write
alike are refused (ValueError) before hashing, so a hash here equals factagora.ai's canonicalJson hash of the same request."""
import hashlib
import json
import math
import re

_MARK = "\ue000"  # private-use char tagging a small number dumped as a string; real strings may not start with it


def _js_safe(x, path: str = "$"):
    """x with integer-valued floats as ints (JS writes 1.0 and -0.0 as 1 and 0) and a non-integer 0 < |x| < 1e-4 as JS
    writes it (_js_small); ValueError on NaN/inf, |x| >= 1e21, a key outside printable ASCII and a string starting with _MARK."""
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
            return _MARK + _js_small(x)
    if isinstance(x, str) and x.startswith(_MARK):
        raise ValueError(f"{path}: a string may not start with U+E000")
    if isinstance(x, int) and not isinstance(x, bool) and abs(x) >= 10**21:
        raise ValueError(f"{path}: {x} is >= 1e21, which JS writes in exponent notation (io-v0 6.4)")
    return x


def _js_small(x: float) -> str:
    """0 < |x| < 1e-4 as JS's Number#toString writes it (io-v0 6.4): decimal from 1e-6 up ("0.000057", Python 5.7e-05),
    else exponent without zero padding ("5.7e-7", Python 5.7e-07). Both use the shortest round-trip digits."""
    m, e = repr(x).split("e")
    sign, m, e = ("-" if m.startswith("-") else ""), m.lstrip("-"), int(e)
    return sign + ("0." + "0" * (-e - 1) + m.replace(".", "") if e >= -6 else f"{m}e{e}")


def canonical(x) -> bytes:
    s = json.dumps(_js_safe(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return re.sub('"' + _MARK + '([^"]*)"', r"\1", s).encode()


def sha256(x) -> str:
    return hashlib.sha256(canonical(x)).hexdigest()


def request_sha256(request: dict) -> str:
    return sha256(request)
