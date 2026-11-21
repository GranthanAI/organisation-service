"""The invite email: subject, IST date, escaping, and no links except the join link."""

import re
from datetime import datetime, timezone

import pytest

from app.email.templates import ist_date, render_invite

LINK = "https://app.granthan.ai/join/AbCdEf-_123456789012345678901234567890123"
EXPIRES = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)        # 01:30 on the 23rd in India


def render(**kw):
    base = dict(inviter_name="Ada Lovelace", org_name="Amity University", role="member", expires_at=EXPIRES, link=LINK)
    return render_invite(**{**base, **kw})


def test_the_subject():
    assert render().subject == "Ada Lovelace invited you to Amity University on GranthanAI"


def test_the_expiry_date_is_shown_in_ist():
    assert ist_date(EXPIRES) == "23 Nov 2026"                                    # not the 22nd, which is the UTC date
    assert "23 Nov 2026" in render().text and "23 Nov 2026" in render().html
    assert ist_date(datetime(2026, 1, 1, 18, 29, tzinfo=timezone.utc)) == "1 Jan 2026"
    assert ist_date(datetime(2026, 1, 1, 18, 30, tzinfo=timezone.utc)) == "2 Jan 2026"


@pytest.mark.parametrize("role, phrase", [("member", "a member"), ("admin", "an admin"), ("owner", "the owner")])
def test_the_role_is_named(role, phrase):
    mail = render(role=role)
    assert phrase in mail.text and phrase in mail.html


def test_text_and_html_have_the_org_inviter_link_expiry_and_the_ignore_line():
    mail = render()
    for body in (mail.text, mail.html):
        assert "Amity University" in body and "Ada Lovelace" in body and "23 Nov 2026" in body
        assert "If you didn't expect this, ignore this email" in body
    assert LINK in mail.text and f'href="{LINK}"' in mail.html


EVIL = '<script>alert("x")</script> & "quotes" \'single\''


def test_every_value_is_html_escaped():
    mail = render(inviter_name=EVIL, org_name=EVIL, role="member", link='https://app/join/x"><img src=//evil/p.png>')
    assert "<script>" not in mail.html and "<img" not in mail.html and "&lt;script&gt;" in mail.html
    assert 'x"><img' not in mail.html and 'x&quot;&gt;&lt;img' in mail.html      # the injected link is neutralised
    assert "&amp;" in mail.html and "&quot;" in mail.html
    assert mail.html.count("<a ") == 1


def test_the_plain_text_part_is_not_escaped_but_has_no_html():
    mail = render(org_name="Smith & Sons")
    assert "Smith & Sons" in mail.text and "&amp;" not in mail.text and "<" not in mail.text.replace("<>", "")


def test_a_name_cannot_inject_headers_through_the_subject():
    mail = render(inviter_name="Eve\r\nBcc: victim@example.com", org_name="Org\nSubject: spam")
    assert "\r" not in mail.subject and "\n" not in mail.subject
    assert mail.subject.startswith("Eve Bcc: victim@example.com invited you to Org Subject: spam on GranthanAI")


def test_no_tracking_pixels_and_no_external_images_or_links_except_the_join_link():
    mail = render()
    assert "<img" not in mail.html and "url(" not in mail.html and "<link" not in mail.html and "<script" not in mail.html
    for body in (mail.text, mail.html):
        urls = set(re.findall(r"https?://[^\s\"'<>]+", body))
        assert urls == {LINK}, urls


def test_it_is_a_complete_html_document():
    html = render().html
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
