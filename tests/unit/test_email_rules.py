import pytest

from app.core.exceptions import InvalidEmail
from app.core.public_email_domains import PUBLIC_EMAIL_DOMAINS
from app.services.email_rules import (
    email_domain,
    is_public_email_domain,
    is_valid_domain,
    is_valid_email,
    normalize_domain,
    normalize_email,
)


@pytest.mark.parametrize("raw, expected", [
    ("  Student@Amity.EDU ", "student@amity.edu"),
    ("a.b+tag@Sub.Example.co.in", "a.b+tag@sub.example.co.in"),
    ("x@xn--bcher-kva.example", "x@xn--bcher-kva.example"),
])
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "no-at-sign", "two@@signs.com", "a@b@c.com", "@amity.edu", "user@", "user@nodot",
    "user@-bad.com", "user@bad-.com", "user@bad..com", "user@amity.e", "user@amity.123",
    "us er@amity.edu", ".lead@amity.edu", "trail.@amity.edu", "do..t@amity.edu",
    "user@exa_mple.com", "user@" + "a" * 64 + ".com", "x" * 65 + "@amity.edu",
    "x@" + ("a" * 60 + ".") * 5 + "com", "ü@amity.edu", "user@bücher.de",
])
def test_normalize_email_rejects(bad):
    with pytest.raises(InvalidEmail):
        normalize_email(bad)
    assert not is_valid_email(bad)


def test_invalid_email_is_a_value_error_with_a_code():
    with pytest.raises(ValueError) as exc:
        normalize_email("nope")
    assert exc.value.code == "VALIDATION_ERROR"


def test_email_domain():
    assert email_domain(" Someone@AMITY.edu ") == "amity.edu"


def test_normalize_domain_trims_lowercases_and_drops_trailing_dot():
    assert normalize_domain("  Amity.EDU. ") == "amity.edu"


@pytest.mark.parametrize("good", ["amity.edu", "cs.amity.edu", "a-b.co.in", "x1.io", "xn--bcher-kva.example"])
def test_valid_domains(good):
    assert is_valid_domain(good)


@pytest.mark.parametrize("bad", [
    "", "amity", ".edu", "amity.", "amity..edu", "Amity.edu", " amity.edu", "-a.edu", "a-.edu",
    "amity.e", "amity.123", "am ity.edu", "a_b.edu", "http://amity.edu", "amity.edu/path", "a" * 64 + ".edu",
    ("a." * 130) + "edu",
])
def test_invalid_domains(bad):
    assert not is_valid_domain(bad)


def test_public_email_domains_are_exactly_the_agreed_set():
    assert PUBLIC_EMAIL_DOMAINS == frozenset({
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
        "icloud.com", "rediffmail.com", "proton.me", "protonmail.com", "zoho.com", "aol.com", "gmx.com", "yandex.com",
    })
    assert isinstance(PUBLIC_EMAIL_DOMAINS, frozenset)


@pytest.mark.parametrize("domain", sorted(PUBLIC_EMAIL_DOMAINS) + ["GMAIL.com", " gmail.com. "])
def test_public_domains_detected(domain):
    assert is_public_email_domain(domain)


@pytest.mark.parametrize("domain", ["amity.edu", "mail.gmail.com", "notgmail.com", "gmail.com.evil.io"])
def test_other_domains_are_not_public(domain):
    assert not is_public_email_domain(domain)
