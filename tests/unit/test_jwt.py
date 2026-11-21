import time
import uuid

import jwt
import pytest

from app.core.config import settings
from app.core.exceptions import Unauthenticated
from app.security.jwt import verify_jwt_token
from tests.support.tokens import make_token


def test_a_valid_token_verifies_and_returns_its_claims():
    user = uuid.uuid4()
    claims = verify_jwt_token(make_token(sub=user, email="a@b.co", roles=["support"], jti="j1"))
    assert claims["sub"] == str(user) and claims["email"] == "a@b.co" and claims["roles"] == ["support"]
    assert claims["jti"] == "j1" and claims["iss"] == "https://auth.granthan.com"


def test_the_dev_issuer_is_what_auth_service_signs_with():
    assert settings.JWT_ISSUER == "https://auth.granthan.com"
    assert settings.JWT_ALGORITHM == "HS256"


def test_expired_is_refused():
    with pytest.raises(Unauthenticated):
        verify_jwt_token(make_token(expires_in=-60))


def test_a_few_seconds_past_expiry_is_within_the_10s_leeway():
    verify_jwt_token(make_token(expires_in=-5))
    with pytest.raises(Unauthenticated):
        verify_jwt_token(make_token(expires_in=-30))


def test_wrong_secret_is_refused():
    with pytest.raises(Unauthenticated):
        verify_jwt_token(make_token(secret="x" * 40))


@pytest.mark.parametrize("iss", ["https://evil.example", "", "auth.granthan.com"])
def test_wrong_issuer_is_refused(iss):
    with pytest.raises(Unauthenticated):
        verify_jwt_token(make_token(iss=iss))


@pytest.mark.parametrize("claim", ["sub", "email", "jti", "exp", "iat", "iss"])
def test_each_required_claim_is_required(claim):
    with pytest.raises(Unauthenticated):
        verify_jwt_token(make_token(omit=(claim,)))


def test_alg_none_is_refused():
    token = make_token(algorithm="none")
    assert token.endswith(".")   # unsigned
    with pytest.raises(Unauthenticated):
        verify_jwt_token(token)


def test_other_algorithms_are_refused_even_with_the_right_secret():
    with pytest.raises(Unauthenticated):
        verify_jwt_token(make_token(algorithm="HS512"))


def test_a_tampered_payload_is_refused():
    head, _payload, sig = make_token().split(".")
    forged = jwt.encode({"sub": str(uuid.uuid4()), "iss": settings.JWT_ISSUER, "exp": int(time.time()) + 99},
                        "other", algorithm="HS256").split(".")[1]
    with pytest.raises(Unauthenticated):
        verify_jwt_token(f"{head}.{forged}.{sig}")


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x", "…"])
def test_garbage_is_refused(garbage):
    with pytest.raises(Unauthenticated):
        verify_jwt_token(garbage)


def test_the_error_says_nothing_about_why():
    for token in (make_token(expires_in=-60), make_token(secret="y" * 40), make_token(iss="x")):
        with pytest.raises(Unauthenticated) as exc:
            verify_jwt_token(token)
        assert exc.value.detail == "Authentication required"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}
        assert exc.value.code == "UNAUTHENTICATED"
