import pytest

from darkstore_common.tokens import InvalidToken, sign, verify

SECRET = "s" * 32


def test_roundtrip_customer_token_is_bound_to_its_order():
    t = sign(SECRET, role="customer", order_id="o-1", ttl_s=60, now=1000)
    c = verify(SECRET, t, now=1010)
    assert (c.role, c.order_id) == ("customer", "o-1")


def test_ops_token_has_no_order():
    c = verify(SECRET, sign(SECRET, role="ops", ttl_s=60, now=0), now=1)
    assert c.role == "ops" and c.order_id is None


def test_expired_token_is_rejected():
    t = sign(SECRET, role="customer", order_id="o", ttl_s=60, now=1000)
    verify(SECRET, t, now=1059)
    with pytest.raises(InvalidToken):
        verify(SECRET, t, now=1061)


def test_wrong_secret_tampered_payload_and_tampered_signature_are_rejected():
    t = sign(SECRET, role="customer", order_id="o-1", ttl_s=60, now=0)
    with pytest.raises(InvalidToken):
        verify("x" * 32, t, now=1)
    body, sig = t.split(".")
    forged = sign(SECRET, role="ops", ttl_s=60, now=0).split(".")[0]      # swap in an ops payload
    with pytest.raises(InvalidToken):
        verify(SECRET, f"{forged}.{sig}", now=1)
    with pytest.raises(InvalidToken):
        verify(SECRET, f"{body}.{sig[:-2]}AA", now=1)


@pytest.mark.parametrize("junk", ["", "abc", "a.b", "a.b.c", ".", None, 12, "é.é"])
def test_malformed_tokens_raise_invalid_token_never_anything_else(junk):
    with pytest.raises(InvalidToken):
        verify(SECRET, junk, now=1)


def test_unknown_role_and_missing_order_for_customer_are_rejected():
    with pytest.raises(ValueError):
        sign(SECRET, role="admin", ttl_s=60)
    with pytest.raises(ValueError):
        sign(SECRET, role="customer", ttl_s=60)                # a customer token needs an order
    with pytest.raises(ValueError):
        sign("", role="ops", ttl_s=60)                         # refuse to sign with an empty secret
    with pytest.raises(InvalidToken):
        verify("", sign(SECRET, role="ops", ttl_s=60), now=1)
