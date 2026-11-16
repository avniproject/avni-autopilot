"""Plain-words helpers for messages shown to the user."""

from __future__ import annotations


def shortened(name: str, keep: int = 60) -> str:
    """The name quoted, cut to its first `keep` characters with an ellipsis."""
    return repr(name if len(name) <= keep else name[:keep].rstrip() + "…")


def first_names(names: list[str], limit: int = 10, separator: str = "; ") -> str:
    """`names` joined, cut to the first `limit` with a count of the rest."""
    shown = separator.join(names[:limit])
    rest = len(names) - limit
    return f"{shown}{separator}and {rest} more" if rest > 0 else shown
