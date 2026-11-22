"""domain_rules: candidate domains, public-domain refusal, normalisation, TXT matching."""

import pytest

from app.core.exceptions import PublicEmailDomain, ValidationFailed
from app.services.domain_rules import (
    candidate_domains,
    check_new_domain,
    covers,
    email_domain,
    is_public_domain,
    txt_matches,
    txt_value,
)


def test_candidate_domains_longest_first_never_the_bare_tld():
    assert candidate_domains("a@cse.student.amity.edu") == ["cse.student.amity.edu", "student.amity.edu", "amity.edu"]
    assert candidate_domains("a@amity.edu") == ["amity.edu"]
    assert candidate_domains("cse.amity.edu") == ["cse.amity.edu", "amity.edu"]        # a bare domain works too
    assert candidate_domains("a@localhost") == [] and candidate_domains("a@com") == []
    assert candidate_domains("A@CSE.Amity.EDU.") == ["cse.amity.edu", "amity.edu"]      # case and a trailing dot


def test_email_domain():
    assert email_domain("Someone@Amity.EDU") == "amity.edu" and email_domain("a@b.co.") == "b.co"


@pytest.mark.parametrize("domain", ["gmail.com", "mail.gmail.com", "a.b.gmail.com", "GMAIL.COM", "yahoo.co.in", "x.yahoo.co.in",
                                    "outlook.com", "proton.me", "mail.proton.me"])
def test_public_domains_and_their_subdomains_are_refused(domain):
    assert is_public_domain(domain)
    with pytest.raises(PublicEmailDomain):
        check_new_domain(domain)


@pytest.mark.parametrize("domain", ["amity.edu", "notgmail.com", "gmail.com.au", "gmail.co", "fakegmail.com", "my-gmail.com"])
def test_lookalikes_of_public_domains_are_not_public(domain):
    assert not is_public_domain(domain)
    assert check_new_domain(domain) == domain


@pytest.mark.parametrize("raw, expected", [("Amity.EDU", "amity.edu"), ("  amity.edu  ", "amity.edu"), ("amity.edu.", "amity.edu"),
                                           ("CSE.Amity.Edu.", "cse.amity.edu"), ("xn--bcher-kva.example", "xn--bcher-kva.example")])
def test_normalisation(raw, expected):
    assert check_new_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "amity", "amity..edu", "-amity.edu", "amity.e", "bücher.example", "ami ty.edu", "a@amity.edu",
                                 "https://amity.edu", "amity.edu/path", "x" * 64 + ".edu", "amity.123"])
def test_invalid_domains(raw):
    with pytest.raises(ValidationFailed):
        check_new_domain(raw)


def test_covers_is_proper_subdomain_only():
    assert covers("amity.edu", "cse.amity.edu") and covers("amity.edu", "a.b.amity.edu")
    assert not covers("amity.edu", "amity.edu") and not covers("amity.edu", "notamity.edu") and not covers("cse.amity.edu", "amity.edu")


def test_txt_matching():
    token = "AbC_123-xyz"
    assert txt_value(token) == "granthan-verify=AbC_123-xyz"
    assert txt_matches(["v=spf1 -all", "granthan-verify=AbC_123-xyz"], token)
    assert txt_matches(["  granthan-verify=AbC_123-xyz "], token)                      # surrounding space is ignored
    assert not txt_matches([], token)
    assert not txt_matches(["granthan-verify=other", "granthan-verify=AbC_123-xyz2", "xgranthan-verify=AbC_123-xyz"], token)
    assert not txt_matches(["granthan-verify=abc_123-xyz"], token)                       # the token is case-sensitive
