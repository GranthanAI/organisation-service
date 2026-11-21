"""Email and domain normalisation. Pure: stdlib and app/core only.

Emails and domains are stored lower-cased and trimmed. Domains are ASCII (IDN domains must arrive as punycode).
"""

import re

from app.core.exceptions import InvalidEmail
from app.core.public_email_domains import PUBLIC_EMAIL_DOMAINS

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^([a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_LOCAL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
MAX_EMAIL_LENGTH = 254


def normalize_domain(domain: str) -> str:
    """Trim, lower-case and drop one trailing dot. Does not validate."""
    d = domain.strip().lower()
    return d[:-1] if d.endswith(".") else d


def is_valid_domain(domain: str) -> bool:
    d = domain
    if not d or len(d) > 253 or d != d.strip().lower():
        return False
    labels = d.split(".")
    if len(labels) < 2:
        return False
    return all(_LABEL.match(label) for label in labels) and bool(_TLD.match(labels[-1]))


def normalize_email(email: str) -> str:
    """Trim and lower-case; raise InvalidEmail unless it is one local part, one `@` and a valid domain."""
    e = email.strip().lower()
    if not e or len(e) > MAX_EMAIL_LENGTH or e.count("@") != 1:
        raise InvalidEmail("not an email address")
    local, domain = e.split("@")
    if (not local or len(local) > 64 or not _LOCAL.match(local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise InvalidEmail("not an email address")
    if not is_valid_domain(domain):
        raise InvalidEmail("not an email address")
    return e


def is_valid_email(email: str) -> bool:
    try:
        normalize_email(email)
    except InvalidEmail:
        return False
    return True


def email_domain(email: str) -> str:
    """The normalised domain of an email."""
    return normalize_email(email).split("@")[1]


def is_public_email_domain(domain: str) -> bool:
    return normalize_domain(domain) in PUBLIC_EMAIL_DOMAINS
