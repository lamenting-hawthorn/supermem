"""Unit tests for PrivacyFilter."""
from __future__ import annotations

import pytest

from recall.privacy.filter import PrivacyFilter


def test_strip_removes_private_block() -> None:
    text = "Hello <private>secret password 123</private> world"
    result = PrivacyFilter.strip(text)
    assert "secret" not in result
    assert "Hello" in result
    assert "world" in result


def test_strip_multiline_private() -> None:
    text = "before\n<private>\nline 1\nline 2\n</private>\nafter"
    result = PrivacyFilter.strip(text)
    assert "line 1" not in result
    assert "before" in result
    assert "after" in result


def test_strip_multiple_blocks() -> None:
    text = "<private>A</private> middle <private>B</private>"
    result = PrivacyFilter.strip(text)
    assert "A" not in result
    assert "B" not in result
    assert "middle" in result


def test_strip_case_insensitive() -> None:
    text = "visible <PRIVATE>hidden</PRIVATE> end"
    result = PrivacyFilter.strip(text)
    assert "hidden" not in result
    assert "visible" in result


def test_strip_no_private_blocks() -> None:
    text = "nothing to strip here"
    result = PrivacyFilter.strip(text)
    assert result == text


def test_strip_all_private_returns_empty() -> None:
    text = "<private>everything is private</private>"
    result = PrivacyFilter.strip(text)
    assert result == ""


def test_has_private_true() -> None:
    assert PrivacyFilter.has_private("hello <private>secret</private> world") is True


def test_has_private_false() -> None:
    assert PrivacyFilter.has_private("nothing private here") is False


def test_redact_replaces_with_placeholder() -> None:
    text = "visible <private>secret</private> end"
    result = PrivacyFilter.redact(text)
    assert "[PRIVATE]" in result
    assert "secret" not in result


def test_redact_custom_replacement() -> None:
    text = "visible <private>secret</private> end"
    result = PrivacyFilter.redact(text, replacement="[REDACTED]")
    assert "[REDACTED]" in result
