"""Short-lived signed tokens (HMAC-SHA256) for tracking and ops access.

token = base64url(json{r: role, o: order_id, e: expiry}) + "." + base64url(hmac)

A customer token is bound to ONE order (the gateway and the order API refuse any other); an ops token
has no order and unlocks the ops API; a guest token carries a server-assigned customer id and may only
create orders (it is useless to the gateway, ops API and every per-order endpoint). Stdlib only, constant-time signature comparison, and
verify() raises InvalidToken for *everything* malformed so callers need one except clause.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

ROLES = ("customer", "ops", "guest")


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class Claims:
    role: str
    order_id: str | None
    expires: int
    customer_id: str | None = None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def sign(secret: str, *, role: str, ttl_s: int, order_id: str | None = None,
         customer_id: str | None = None, now: float | None = None) -> str:
    if not secret:
        raise ValueError("refusing to sign with an empty secret (set TOKEN_SECRET)")
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if role == "customer" and not order_id:
        raise ValueError("a customer token must be bound to an order")
    if role == "guest" and not customer_id:
        raise ValueError("a guest token must carry a customer id")
    exp = int((time.time() if now is None else now) + ttl_s)
    claims = {"r": role, "o": order_id, "e": exp}
    if customer_id:
        claims["c"] = customer_id
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{body}.{_mac(secret, body)}"


def verify(secret: str, token: object, now: float | None = None) -> Claims:
    try:
        if not secret or not isinstance(token, str):
            raise InvalidToken("bad input")
        body, sig = token.split(".")
        if not hmac.compare_digest(sig, _mac(secret, body)):
            raise InvalidToken("bad signature")
        d = json.loads(_unb64(body))
        role, order_id, exp, cust = d["r"], d["o"], int(d["e"]), d.get("c")
        if role not in ROLES or (role == "customer" and not order_id) or (role == "guest" and not cust):
            raise InvalidToken("bad claims")
    except InvalidToken:
        raise
    except Exception as e:  # noqa: BLE001 - any malformation is simply "invalid"
        raise InvalidToken("malformed token") from e
    if (time.time() if now is None else now) > exp:
        raise InvalidToken("expired")
    return Claims(role, order_id, exp, cust)
