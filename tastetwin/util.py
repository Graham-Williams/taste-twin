"""Small shared helpers."""

from __future__ import annotations

import re

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def safe_filename(name: str) -> str:
    """Sanitize an untrusted name for use as a single path component.

    Replaces anything outside [A-Za-z0-9_.-] with '_' and neutralizes
    dot-only results ('.', '..', '...') so a hostile name can never
    traverse out of its parent directory.
    """
    cleaned = _SAFE_NAME.sub("_", name)
    if not cleaned.strip("."):
        cleaned = "_" * max(len(cleaned), 1)
    return cleaned
