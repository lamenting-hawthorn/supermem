"""PrivacyFilter — strips <private>...</private> blocks from content.

Content inside <private> tags is NEVER written to SQLite, Kuzu, or Chroma and
is removed from ordinary vault reads exposed to the restricted local executor.

Apache 2.0 — original implementation.
"""

from __future__ import annotations

import re

_PRIVATE_TAG_RE = re.compile(r"</?private\s*>", re.IGNORECASE)


class PrivacyFilter:
    """Stateless utility for stripping private content."""

    @staticmethod
    def strip(text: str) -> str:
        """Remove nested private blocks; an unclosed opener strips through EOF.

        Storage callers use this single boundary before persistence.  Treating an
        unclosed opener as private is intentional: malformed hostile input must
        never turn a private suffix into indexed content.
        """
        parts: list[str] = []
        cursor = 0
        depth = 0
        for match in _PRIVATE_TAG_RE.finditer(text):
            if depth == 0:
                parts.append(text[cursor : match.start()])
            tag = match.group(0).lower()
            if tag.startswith("</"):
                if depth == 0:
                    # A stray closer means the remaining structure is uncertain.
                    # Keep only the already-known public prefix rather than risk
                    # persisting a private suffix.
                    return "".join(parts).strip()
                depth -= 1
            else:
                depth += 1
            cursor = match.end()
        if depth == 0:
            parts.append(text[cursor:])
        return "".join(parts).strip()

    @staticmethod
    def has_private(text: str) -> bool:
        """Return True if text contains any <private> blocks."""
        return bool(_PRIVATE_TAG_RE.search(text))

    @staticmethod
    def strip_preserving_public_bytes(text: str) -> str:
        """Remove private blocks without rewriting adjacent public bytes.

        Navigation callers use this variant when returning imported vault content:
        private content and tags are removed, while known-public prefixes and
        suffixes remain byte-for-byte unchanged.  An unclosed opener or stray
        closer leaves only the already-known public prefix.
        """
        parts: list[str] = []
        cursor = 0
        depth = 0
        for match in _PRIVATE_TAG_RE.finditer(text):
            if depth == 0:
                parts.append(text[cursor : match.start()])
            tag = match.group(0).lower()
            if tag.startswith("</"):
                if depth == 0:
                    return "".join(parts)
                depth -= 1
            else:
                depth += 1
            cursor = match.end()
        if depth == 0:
            parts.append(text[cursor:])
        return "".join(parts)

    @staticmethod
    def redact(text: str, replacement: str = "[PRIVATE]") -> str:
        """Replace private blocks with a placeholder (useful for logging)."""
        parts: list[str] = []
        cursor = 0
        depth = 0
        for match in _PRIVATE_TAG_RE.finditer(text):
            if depth == 0:
                parts.append(text[cursor : match.start()])
            tag = match.group(0).lower()
            if tag.startswith("</"):
                if depth == 0:
                    return "".join(parts)
                depth -= 1
            else:
                if depth == 0:
                    parts.append(replacement)
                depth += 1
            cursor = match.end()
        if depth == 0:
            parts.append(text[cursor:])
        return "".join(parts)
