"""LLM input parser — Gemma 3 27B via Gemini API (v1.5 fallback).

Only called when regex fails and GEMINI_API_KEY is set. Produces structured
ParsedInput; never influences card recommendations.
"""
from __future__ import annotations

import json
import os

from google import genai
from google.genai import types
from pydantic import BaseModel

from .models import ParsedInput


class _ParseResult(BaseModel):
    """Schema class for Gemini response_schema — no gt/exclusiveMinimum constraints."""
    merchant: str
    amount_sgd: float
    is_fcy: bool

_SYSTEM_PROMPT = (
    "You parse transaction input for a miles card recommender. "
    "Extract: merchant (lowercase, spaces become underscores), "
    "amount_sgd (positive float, SGD value), "
    "is_fcy (true only if the 'fcy' keyword is present). "
    "Return only a JSON object with exactly these keys. Examples: "
    "'Cold Storage 45' → {\"merchant\":\"cold_storage\",\"amount_sgd\":45.0,\"is_fcy\":false}; "
    "'klook 320 fcy' → {\"merchant\":\"klook\",\"amount_sgd\":320.0,\"is_fcy\":true}; "
    "'don don donki 88.50' → {\"merchant\":\"don_don_donki\",\"amount_sgd\":88.5,\"is_fcy\":false}"
)

_MODEL = "gemma-4-31b-it"
_client: genai.Client | None = None


def is_enabled() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def _extract(raw: str) -> ParsedInput:
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("LLM returned no parseable JSON")
    data = json.loads(raw[start:end])
    return ParsedInput.model_validate(data)


async def llm_parse(text: str) -> ParsedInput:
    client = _get_client()

    # Attempt 1: structured output with JSON schema constraint
    try:
        resp = await client.aio.models.generate_content(
            model=_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_ParseResult,
            ),
        )
        return _extract(resp.text)
    except Exception:
        pass

    # Attempt 2: free-text response, extract JSON manually
    resp = await client.aio.models.generate_content(
        model=_MODEL,
        contents=f"Parse this transaction input and return only JSON: {text}",
        config=types.GenerateContentConfig(system_instruction=_SYSTEM_PROMPT),
    )
    return _extract(resp.text)
