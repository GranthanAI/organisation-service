import hashlib
import re

from app.security.tokens import hash_token, tokens_equal


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert re.fullmatch(r"[0-9a-f]{64}", hash_token("anything"))


def test_hashes_differ_per_token_and_are_not_the_token():
    tokens = [f"token-{i}" for i in range(200)]
    hashes = {hash_token(t) for t in tokens}
    assert len(hashes) == 200
    assert all(t not in hash_token(t) for t in tokens)


def test_hash_is_deterministic():
    assert hash_token("same") == hash_token("same") != hash_token("other")


def test_tokens_equal():
    assert tokens_equal(hash_token("a"), hash_token("a"))
    assert not tokens_equal(hash_token("a"), hash_token("b"))
    assert not tokens_equal("", hash_token("a"))
