"""
Text transforms for TTS preprocessing.

Each transform is an async generator that receives chunks of LLM output
and yields cleaned text suitable for speech synthesis.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable

from num2words import num2words

__all__ = ["digits_to_words"]

_DIGIT_RE = re.compile(r"\d+")


def _spell_digits(m: re.Match[str]) -> str:
    """Spell a digit run digit-by-digit (``"4521"`` → "four five two one")."""
    return " ".join(num2words(int(d), lang="ru") for d in m.group())


async def digits_to_words(text: AsyncIterable[str]) -> AsyncIterable[str]:
    """Replace digit sequences with Russian word forms, digit-by-digit.

    Silero TTS cannot pronounce digits (1 → "один").  This transform
    is a safety net — the LLM is prompted to write numbers as words, so
    any raw digits that slip through are almost always identifiers
    (phone numbers, codes, OTPs) where reading each digit individually
    is the correct behaviour. Leading zeros are preserved (``"007"`` →
    "zero zero seven").
    """
    async for chunk in text:
        yield _DIGIT_RE.sub(_spell_digits, chunk)
