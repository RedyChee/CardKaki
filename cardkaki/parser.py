"""Strict regex parser: '<merchant> <amount> [fcy|sgd]'.

LLM fallback is v1.5. v1 is regex-strict so users learn the format and
the bot stays fast/free.
"""
from __future__ import annotations

import re

from .models import ParsedInput

_PATTERN = re.compile(
    r"^\s*(?P<merchant>[a-zA-Z][a-zA-Z0-9_\-' ]*?)"
    r"\s+(?P<amount>\d+(?:\.\d{1,2})?)"
    r"(?:\s+(?P<flag>fcy|sgd))?\s*$",
    re.IGNORECASE,
)


def parse(text: str) -> ParsedInput:
    if not text or not text.strip():
        raise ValueError(_HINT)
    m = _PATTERN.match(text)
    if not m:
        raise ValueError(_HINT)
    merchant = m.group("merchant").strip().lower().replace(" ", "_")
    return ParsedInput(
        merchant=merchant,
        amount_sgd=float(m.group("amount")),
        is_fcy=(m.group("flag") or "").lower() == "fcy",
    )


_HINT = (
    "Couldn't parse. Try: <merchant> <amount> [fcy], "
    "e.g. 'cold storage 45' or 'klook 320 fcy'"
)
