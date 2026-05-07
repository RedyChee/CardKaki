"""Tests for parse_async — mocked Gemini client, no real API calls."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cardkaki.models import ParsedInput
from cardkaki.parser import parse_async


@pytest.mark.asyncio
async def test_regex_still_works_without_llm_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = await parse_async("cold storage 45")
    assert result.merchant == "cold_storage"
    assert result.amount_sgd == 45.0
    assert result.is_fcy is False


@pytest.mark.asyncio
async def test_regex_failure_no_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        await parse_async("coldstorage")  # no amount — regex fails


@pytest.mark.asyncio
async def test_llm_rescues_typo(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_resp = MagicMock()
    mock_resp.text = '{"merchant":"cold_storage","amount_sgd":45.0,"is_fcy":false}'

    mock_generate = AsyncMock(return_value=mock_resp)

    with patch("cardkaki.llm_parser._get_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_generate
        mock_client_fn.return_value = mock_client

        # "coldstorage 45" fails regex (no space before number in single-word merchant is fine,
        # but let's use something that truly fails regex)
        result = await parse_async("cold storage @ 45")  # @ breaks regex
        assert result.merchant == "cold_storage"
        assert result.amount_sgd == 45.0
        assert result.is_fcy is False


@pytest.mark.asyncio
async def test_llm_failure_raises_original_regex_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("cardkaki.llm_parser._get_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=Exception("API down"))
        mock_client_fn.return_value = mock_client

        with pytest.raises(ValueError, match="Couldn't parse"):
            await parse_async("cold storage @ 45")


@pytest.mark.asyncio
async def test_llm_fcy_flag(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_resp = MagicMock()
    mock_resp.text = '{"merchant":"klook","amount_sgd":320.0,"is_fcy":true}'

    with patch("cardkaki.llm_parser._get_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        mock_client_fn.return_value = mock_client

        result = await parse_async("klook @ 320 fcy")
        assert result.merchant == "klook"
        assert result.is_fcy is True
