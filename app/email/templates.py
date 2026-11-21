"""The invite email: plain text and HTML. Every value is HTML-escaped; no tracking pixels, no external images, and the
only link is the join link."""

import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))   # a fixed offset: India has no daylight saving
ROLE_PHRASE = {"owner": "the owner", "admin": "an admin", "member": "a member"}


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


def ist_date(moment: datetime) -> str:
    """23 Nov 2026 (the date in India)."""
    local = moment.astimezone(IST)
    return f"{local.day} {local:%b} {local.year}"


def _one_line(value: str) -> str:
    """No line breaks in a header value (a name could try to inject headers)."""
    return re.sub(r"\s+", " ", value).strip()


def render_invite(*, inviter_name: str, org_name: str, role: str, expires_at: datetime, link: str) -> RenderedEmail:
    inviter, org = _one_line(inviter_name), _one_line(org_name)
    role_phrase = ROLE_PHRASE.get(role, "a member")
    expiry = ist_date(expires_at)
    subject = f"{inviter} invited you to {org} on GranthanAI"
    text = (
        f"{inviter} invited you to join {org} on GranthanAI as {role_phrase}.\n\n"
        f"Accept the invitation: {link}\n\n"
        f"This invitation expires on {expiry} (India time).\n\n"
        "If you didn't expect this, ignore this email.\n"
    )
    e = html.escape
    body = (
        f"<p><strong>{e(inviter)}</strong> invited you to join <strong>{e(org)}</strong> on GranthanAI "
        f"as {e(role_phrase)}.</p>"
        f'<p><a href="{e(link, quote=True)}">Accept the invitation</a></p>'
        f"<p>This invitation expires on {e(expiry)} (India time).</p>"
        "<p>If you didn't expect this, ignore this email.</p>"
    )
    page = f'<!doctype html><html><body style="font-family: sans-serif">{body}</body></html>'
    return RenderedEmail(subject=subject, text=text, html=page)
