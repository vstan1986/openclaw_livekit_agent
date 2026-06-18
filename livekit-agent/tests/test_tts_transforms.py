"""
Tests for TTS text transforms — digit-by-digit spelling.

``digits_to_words`` must read every digit individually (phone numbers,
codes, OTPs) and preserve leading zeros, since Silero cannot pronounce
raw digits.
"""

from __future__ import annotations

from collections.abc import AsyncIterable

import pytest

from my_agent.plugin_tts_transforms import digits_to_words


async def _collect(text: str) -> str:
    async def _gen() -> AsyncIterable[str]:
        yield text

    out = ""
    async for chunk in digits_to_words(_gen()):
        out += chunk
    return out


async def test_multi_digit_is_spelled_digit_by_digit() -> None:
    assert await _collect("4521") == "четыре пять два один"


async def test_leading_zeros_preserved() -> None:
    assert await _collect("007") == "ноль ноль семь"


async def test_phone_number_all_digits() -> None:
    out = await _collect("79991234567")
    # 11 digits → 11 spelled words.
    assert len(out.split()) == 11
    assert out.startswith("семь девять девять девять")


async def test_digits_inside_text() -> None:
    assert await _collect("код 45") == "код четыре пять"


async def test_no_digits_unchanged() -> None:
    assert await _collect("привет, как дела?") == "привет, как дела?"


async def test_single_digit() -> None:
    assert await _collect("5") == "пять"


@pytest.mark.parametrize("digit,word", [
    ("0", "ноль"),
    ("1", "один"),
    ("9", "девять"),
])
async def test_each_digit(digit: str, word: str) -> None:
    assert await _collect(digit) == word
