"""PrivacyFilter — strips <private>...</private> blocks from content.

Content inside <private> tags is NEVER written to SQLite, Kuzu, or Chroma.
It is visible only to the agent during its navigation pass.

Apache 2.0 — original implementation.
"""

from __future__ import annotations

import re

# Matches the innermost <private>...</private> block — one that contains no nested opener.
# Looping with this regex handles arbitrarily deep nesting.
_PRIVATE_RE = re.compile(r"<private>(?:(?!<private>)[\s\S])*?</private>", re.IGNORECASE)


class PrivacyFilter:
    """Stateless utility for stripping private content."""

    @staticmethod
    def strip(text: str) -> str:
        """Remove all <private>...</private> blocks, including nested ones."""
        # Loop until no matches remain — handles nested tags like
        # <private>outer<private>inner</private>rest</private>
        prev = None
        result = text
        while prev != result:
            prev = result
            result = _PRIVATE_RE.sub("", result)
        return result.strip()

    @staticmethod
    def has_private(text: str) -> bool:
        """Return True if text contains any <private> blocks."""
        return bool(_PRIVATE_RE.search(text))

    @staticmethod
    def redact(text: str, replacement: str = "[PRIVATE]") -> str:
        """Replace private blocks with a placeholder (useful for logging)."""
        return _PRIVATE_RE.sub(replacement, text)
