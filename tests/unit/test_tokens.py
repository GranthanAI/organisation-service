import hashlib
import re

from app.security.tokens import hash_token, new_token, tokens_equal


def test_new_token_returns_raw_and_its_sha256():
    raw, digest = new_token()
    assert digest == hashlib.sha256(raw.encode()).hexdigest()
    assert hash_token(raw) == digest
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_raw_token_has_32_bytes_of_entropy_and_is_url_safe():
    raw, _ = new_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", raw)  # 32 bytes, base64url, no padding


def test_tokens_and_hashes_differ_per_token():
    pairs = [new_token() for _ in range(200)]
    assert len({raw for raw, _ in pairs}) == 200
    assert len({digest for _, digest in pairs}) == 200


def test_hash_does_not_contain_the_token_and_is_not_the_token():
    raw, digest = new_token()
    assert raw not in digest and digest != raw
    assert hash_token(raw + "x") != digest  # one changed character changes the whole hash


def test_hash_is_deterministic():
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_tokens_equal():
    raw, digest = new_token()
    assert tokens_equal(hash_token(raw), digest)
    assert not tokens_equal(hash_token("other"), digest)
    assert not tokens_equal("", digest)
