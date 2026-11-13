"""Redaction tests: a corpus of secret shapes, identifier negatives and config wiring.

Secret-looking samples are built by concatenation on purpose: the public-safety scanner must not
find literal credential shapes in this repository, but the tests still need to exercise them.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from context_bridge.config import RedactionConfig, parse_config
from context_bridge.redact import MASK, RedactionResult, Redactor

# -- sample builders (no literal secret shapes in source) ---------------------------------


def openai_key() -> str:
    return "s" + "k-" + "proj-" + "Ab3" * 12


def github_token() -> str:
    return "gh" + "p_" + "A1b2C3d4E5f6" * 3


def github_pat() -> str:
    return "git" + "hub_pat_" + "Ab3_" * 10


def aws_access_key() -> str:
    return "AK" + "IA" + "A1B2C3D4E5F6G7H8"


def slack_token() -> str:
    return "xox" + "b-" + "123456789012-AbCdEfGh"


def jwt_token() -> str:
    return (
        "eyJ"
        + "hbGciOiJIUzI1NiJ9"
        + "."
        + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        + "."
        + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )


def pem_block() -> str:
    return "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END " + "RSA PRIVATE KEY-----"


def feishu_chat_id() -> str:
    return "o" + "c_" + "0123456789abcdef0123456789abcdef"


def feishu_user_id() -> str:
    return "o" + "u_" + "fedcba9876543210fedcba9876543210"


def random_blob() -> str:
    """A deterministic, random-looking 44-char base64 token (needs no literal secret)."""
    return base64.urlsafe_b64encode(hashlib.sha256(b"context-bridge-test-vector").digest()).decode()


def git_sha() -> str:
    return hashlib.sha1(b"context-bridge-sha").hexdigest()


def long_hex_id(length: int = 32) -> str:
    return hashlib.sha256(b"context-bridge-id").hexdigest()[:length]


def private_ip() -> str:
    return "10." + "1.2.3"


def private_ip_172() -> str:
    return "172." + "20.0.9"


def _corpus() -> list[tuple[str, str, str, str]]:
    """``(name, text, secret_fragment, expected_rule)``."""
    return [
        ("openai_key", f"OPENAI_API_KEY={openai_key()}", openai_key(), "openai_key"),
        ("github_token", f"token: {github_token()}", github_token(), "github_token"),
        ("github_pat", f"auth {github_pat()} end", github_pat(), "github_token"),
        ("aws_access_key", f"AWS_ACCESS_KEY_ID={aws_access_key()}", aws_access_key(), "aws_access_key"),
        ("slack_token", f"hook {slack_token()} now", slack_token(), "slack_token"),
        ("jwt", f"Authorization: {jwt_token()}", jwt_token(), "jwt"),
        ("pem_private_key", f"key:\n{pem_block()}\ndone", "MIIEowIBAAKCAQEA", "pem_private_key"),
        (
            "bearer",
            "Authorization: " + "Bearer " + "abcDEF1234567890xyz",
            "abcDEF1234567890xyz",
            "bearer_token",
        ),
        (
            "basic_auth_url",
            "https://deploy:" + "pass123" + "@example.com/repo.git",
            "pass123",
            "basic_auth_url",
        ),
        (
            "password_assignment",
            "database password = " + "s3cr3tValue!",
            "s3cr3tValue!",
            "credential_assignment",
        ),
        (
            "api_key_assignment",
            'config {"api_key": "' + "Zx9AbCdEf123456" + '"}',
            "Zx9AbCdEf123456",
            "credential_assignment",
        ),
        (
            "cli_secret_argument",
            "run collector --token " + "abcdef123456",
            "abcdef123456",
            "cli_secret_argument",
        ),
        ("loopback_url", "http://127.0.0.1:8080/api/health", "127.0.0.1:8080", "loopback_url"),
        ("loopback_endpoint", "connect to " + "localhost" + ":3000 now", "localhost:3000", "loopback_url"),
        ("private_ip", f"host {private_ip()} inside", private_ip(), "private_ip"),
        ("private_ip_172", f"host {private_ip_172()} inside", private_ip_172(), "private_ip"),
        (
            "internal_domain",
            "build.corp.example.com",
            "build.corp.example.com",
            "internal_domain",
        ),
        ("internal_host", "http://svc.internal/status", "svc.internal", "internal_host"),
        ("high_entropy_secret", f"blob={random_blob()}", random_blob(), "high_entropy_secret"),
    ]


# -- tests -------------------------------------------------------------------------------


def test_secret_corpus_never_survives() -> None:
    redactor = Redactor()
    for name, text, fragment, expected_rule in _corpus():
        result = redactor.clean(text)
        assert isinstance(result, RedactionResult)
        assert fragment not in result.text, f"{name}: secret survived as {result.text!r}"
        assert MASK in result.text, f"{name}: no mask marker in {result.text!r}"
        assert result.count >= 1, f"{name}: nothing counted"
        assert expected_rule in result.rules, f"{name}: got rules {result.rules}"


def test_identifiers_are_preserved() -> None:
    redactor = Redactor()
    positives = [
        "commit " + git_sha(),
        "sha256 " + hashlib.sha256(b"x").hexdigest(),
        "record opus:ticket:" + long_hex_id(),
        "uuid 123e4567-e89b-12d3-a456-426614174000",
        "chat " + feishu_chat_id() + " user " + feishu_user_id(),
        "branch fix/expg-allowout-host-sanitize-20261101",
        "file application/json and superagent-worker",
        "model claude-sonnet-4-6-cc",
        "https://example.com/docs/configuration-reference",
        "created 2026-11-13T20:00:25.000Z by agent@example.com",
        "路径 工作目录变更说明",
    ]
    for text in positives:
        result = redactor.clean(text)
        assert result.count == 0, f"false positive on {text!r}: {result.rules} -> {result.text!r}"
        assert result.text == text


def test_clean_is_idempotent() -> None:
    redactor = Redactor()
    text = f"token {openai_key()} and password = hunter2xyz for {private_ip()}"
    first = redactor.clean(text)
    second = redactor.clean(first.text)
    assert first.count >= 2
    assert second.count == 0
    assert second.text == first.text


def test_clean_obj_recurses_dicts_and_lists() -> None:
    redactor = Redactor()
    value = {
        "title": "hello",
        "nested": {"key": openai_key(), "items": ["safe", f"password = {'hunter2'}", 42]},
        "list": [{"deep": f"host {private_ip()}"}],
    }
    cleaned, count = redactor.clean_obj(value)
    assert count >= 3
    assert cleaned["title"] == "hello"
    assert cleaned["nested"]["key"] == MASK
    assert cleaned["nested"]["items"][0] == "safe"
    assert MASK in cleaned["nested"]["items"][1]
    assert MASK in cleaned["list"][0]["deep"]
    assert cleaned["nested"]["items"][2] == 42
    # The input object is not mutated.
    assert value["nested"]["key"] == openai_key()


def test_stats_are_per_rule() -> None:
    redactor = Redactor()
    assert redactor.stats() == {"total": 0, "by_rule": {}}
    redactor.clean(f"{openai_key()} + {openai_key()}")
    redactor.clean(f"host {private_ip()}")
    stats = redactor.stats()
    assert stats["total"] == 3
    assert stats["by_rule"]["openai_key"] == 2
    assert stats["by_rule"]["private_ip"] == 1


def test_disabled_redactor_passes_text_through() -> None:
    redactor = Redactor(RedactionConfig(enabled=False))
    text = f"token {openai_key()}"
    result = redactor.clean(text)
    assert result.text == text
    assert result.count == 0


def test_private_ip_toggle() -> None:
    redactor = Redactor(RedactionConfig(mask_private_ips=False))
    private = f"host {private_ip()}"
    assert redactor.clean(private).text == private
    # Loopback URLs are masked regardless: they are endpoints, not only addresses.
    loopback = "http://127.0.0.1:9000/x"
    assert MASK in redactor.clean(loopback).text


def test_internal_domain_config_is_honoured() -> None:
    redactor = Redactor(RedactionConfig(mask_internal_domains=["acme.example"]))
    result = redactor.clean("deploy on api.acme.example today")
    assert MASK in result.text
    assert "acme.example" not in result.text
    # An unrelated public domain is untouched.
    public = "docs at https://example.com/reference"
    assert redactor.clean(public).text == public


def test_extra_patterns_and_invalid_regex() -> None:
    redactor = Redactor(RedactionConfig(extra_patterns=[r"PROJECT-\d{4}", "["]))
    result = redactor.clean("see PROJECT-1234 and literal [ bracket")
    assert "PROJECT-1234" not in result.text
    assert MASK in result.text
    # A malformed pattern is skipped (logged), never applied to every character.
    assert "literal [ bracket" in result.text


def test_full_config_wiring() -> None:
    config = parse_config(
        {
            "hub": {"dir": "/tmp/hub", "commit": False},
            "bots": {"alpha": {"state_dir": "/tmp/state-a"}},
            "redaction": {
                "enabled": True,
                "mask_private_ips": True,
                "mask_internal_domains": ["corp.example.com"],
                "extra_patterns": [r"internal-token-\w+"],
            },
        }
    )
    redactor = Redactor(config)
    text = f"api {openai_key()} on {private_ip()} at build.corp.example.com with internal-token-abc"
    result = redactor.clean(text)
    assert result.count >= 4
    for fragment in (openai_key(), private_ip(), "corp.example.com", "internal-token-abc"):
        assert fragment not in result.text


def test_mask_placeholder_has_no_secret_shape() -> None:
    # Cleaning the mask itself must be a no-op: the pipeline can see already-cleaned text.
    assert Redactor().clean(MASK).count == 0
    assert Redactor().clean(f"password = {MASK}").count == 0


@pytest.mark.parametrize(
    "text",
    [
        "password: " + "hunter2hunter2",
        "PASSWORD=" + "hunter2hunter2",
        '"secret": "' + "hunter2hunter2" + '"',
        "client_secret = " + "hunter2hunter2",
        "access_token: " + "hunter2hunter2",
    ],
)
def test_credential_assignment_variants(text: str) -> None:
    result = Redactor().clean(text)
    assert "hunter2hunter2" not in result.text
    assert MASK in result.text
