"""Strict regex parser: '<merchant> <amount> [fcy|sgd]'.

v1.5: if regex fails and GEMINI_API_KEY is set, falls back to Gemma 3 27B
via the Gemini API. LLM only produces structured input; card logic stays
deterministic.
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

_HINT = (
    "Couldn't parse. Try: <merchant> <amount> [fcy], "
    "e.g. 'cold storage 45' or 'klook 320 fcy'"
)


def _parse_regex(text: str) -> ParsedInput:
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


parse = _parse_regex  # sync alias — used by tests and any direct callers


async def parse_async(text: str) -> ParsedInput:
    try:
        return _parse_regex(text)
    except ValueError as regex_err:
        from .llm_parser import is_enabled, llm_parse

        if not is_enabled():
            raise
        try:
            return await llm_parse(text)
        except Exception:
            raise regex_err
