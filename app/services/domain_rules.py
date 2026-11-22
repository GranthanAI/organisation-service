"""Email-domain rules (Phase 9). Pure: stdlib and app/core only.

A domain is stored lower-case, ASCII (punycode for IDN), without a trailing dot. A verified domain matches an email
exactly, or, with `include_subdomains`, every subdomain of it (cse.amity.edu for amity.edu). The longest match wins.
"""

from app.core.exceptions import PublicEmailDomain, ValidationFailed
from app.core.public_email_domains import PUBLIC_EMAIL_DOMAINS
from app.services.email_rules import is_valid_domain, normalize_domain

VERIFY_PREFIX = "granthan-verify="
MAX_DOMAINS_PER_ORG = 10


def email_domain(email: str) -> str:
    """The domain part of an email address, lower-case."""
    return normalize_domain(email.rpartition("@")[2])


def candidate_domains(email_or_domain: str) -> list[str]:
    """The domains an email could be verified under, longest first, never the bare TLD:
    a@cse.student.amity.edu -> [cse.student.amity.edu, student.amity.edu, amity.edu]."""
    domain = email_domain(email_or_domain) if "@" in email_or_domain else normalize_domain(email_or_domain)
    labels = [label for label in domain.split(".") if label]
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def is_public_domain(domain: str) -> bool:
    """A public mail domain (gmail.com), or a subdomain of one (mail.gmail.com)."""
    d = normalize_domain(domain)
    return any(d == public or d.endswith("." + public) for public in PUBLIC_EMAIL_DOMAINS)


def covers(parent: str, child: str) -> bool:
    """True if `child` is a proper subdomain of `parent`."""
    return child.endswith("." + parent) and child != parent


def check_new_domain(raw: str) -> str:
    """Normalise a domain someone wants to add. ValidationFailed if it is not a valid ASCII domain (an IDN must be
    given as punycode), PublicEmailDomain if it is a public mail domain or a subdomain of one."""
    domain = normalize_domain(raw)
    if not is_valid_domain(domain):
        raise ValidationFailed("not a valid domain (use punycode for international names)")
    if is_public_domain(domain):
        raise PublicEmailDomain("public mail domains cannot be verified by an organisation")
    return domain


def check_domain_name(raw: str) -> str:
    """Normalise a domain given in a URL path (no public-domain check: it names a domain that already exists)."""
    return normalize_domain(raw)


def txt_value(token: str) -> str:
    return VERIFY_PREFIX + token


def txt_matches(records: list[str], token: str) -> bool:
    """True if any TXT record (its strings already joined) equals granthan-verify=<token>."""
    wanted = txt_value(token)
    return any(record.strip() == wanted for record in records)
