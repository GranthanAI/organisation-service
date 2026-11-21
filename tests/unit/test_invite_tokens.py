"""Invite tokens are derived (HMAC), never stored: deterministic per (id, version); a version bump gives a new token."""

import base64
import re
import uuid

from app.core.config import settings
from app.security.tokens import hash_token, invite_token, invite_token_hash

SECRET = "s" * 40
INVITE = uuid.UUID("01a1ff67-07db-776a-b01c-fb0daa4d0c4b")


def test_the_token_is_deterministic_per_invite_and_version():
    assert invite_token(INVITE, 1, SECRET) == invite_token(INVITE, 1, SECRET)
    assert invite_token(INVITE, 1) == invite_token(INVITE, 1, settings.INVITE_TOKEN_SECRET)     # the default is the setting


def test_a_version_change_gives_a_new_token():
    tokens = [invite_token(INVITE, v, SECRET) for v in range(1, 8)]
    assert len(set(tokens)) == 7
    assert invite_token(INVITE, 1, SECRET) != invite_token(INVITE, 2, SECRET)


def test_another_invite_gets_another_token():
    assert invite_token(INVITE, 1, SECRET) != invite_token(uuid.uuid4(), 1, SECRET)
    assert len({invite_token(uuid.uuid4(), 1, SECRET) for _ in range(300)}) == 300


def test_a_secret_change_invalidates_every_token():
    assert invite_token(INVITE, 1, "a" * 40) != invite_token(INVITE, 1, "b" * 40)
    assert invite_token_hash(INVITE, 1, "a" * 40) != invite_token_hash(INVITE, 1, "b" * 40)


def test_the_hash_differs_from_the_token_and_is_sha256_of_it():
    token = invite_token(INVITE, 1, SECRET)
    digest = invite_token_hash(INVITE, 1, SECRET)
    assert digest == hash_token(token) and digest != token and token not in digest
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_the_token_is_url_safe_base64_of_32_bytes_without_padding():
    token = invite_token(INVITE, 1, SECRET)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token) and "=" not in token
    assert len(base64.urlsafe_b64decode(token + "=")) == 32


def test_the_token_does_not_contain_the_invite_id_or_the_secret():
    token = invite_token(INVITE, 1, SECRET)
    assert str(INVITE) not in token and SECRET not in token and INVITE.hex not in token


def test_a_different_version_of_the_same_invite_has_a_different_hash_so_the_old_link_dies():
    assert invite_token_hash(INVITE, 1, SECRET) != invite_token_hash(INVITE, 2, SECRET)
