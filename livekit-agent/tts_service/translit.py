"""
Latin → Cyrillic transliteration for Russian (ISO 9 + common mappings).

Converts Latin letters to Cyrillic so that Silero v5_5_ru can pronounce
English words (with a Russian accent).

Algorithm: longest-prefix match — digraphs (sh → ш, ch → ч, zh → ж, …)
are checked before single letters.
"""

from __future__ import annotations

import re

# Digraphs checked BEFORE single letters (order matters: more specific first).
_DIGRAPHS: dict[str, str] = {
    "shch": "щ",  # must be before sh
    "sh": "ш",
    "ch": "ч",
    "ck": "к",  # track → трак, not тракк
    "zh": "ж",
    "kh": "х",
    "ts": "ц",
    "ya": "я",
    "yu": "ю",
    "ye": "е",  # ye- word-initially → е
    "yo": "ё",
}

# Single letter mappings.
_SINGLE: dict[str, str] = {
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "й",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "и",
    "z": "з",
}

# Combined map: digraphs first (longest-prefix), then single letters.
_TRANSLIT_MAP: list[tuple[str, str]] = [
    *sorted(_DIGRAPHS.items(), key=lambda x: -len(x[0])),
    *sorted(_SINGLE.items(), key=lambda x: -len(x[0])),
]


def latin_to_cyrillic(text: str) -> str:
    """Transliterate Latin letters in *text* to Cyrillic.

    Args:
        text: Input text (may contain Latin, Cyrillic, digits).

    Returns:
        Text with Latin letters replaced by Cyrillic equivalents.
        Cyrillic, digits, and punctuation are left unchanged.
    """
    # Fast path: no Latin letters → return as-is
    if not re.search(r"[a-zA-Z]", text):
        return text

    result: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if not ch.isascii() or not ch.isalpha():
            result.append(ch)
            i += 1
            continue

        # Case-insensitive longest-prefix match
        upper = ch.isupper()
        matched = False
        for pattern, cyr in _TRANSLIT_MAP:
            if text[i : i + len(pattern)].lower() == pattern:
                replacement = cyr.capitalize() if upper and not cyr.startswith("ь") else cyr
                result.append(replacement)
                i += len(pattern)
                matched = True
                break

        if not matched:
            result.append(ch)
            i += 1

    return "".join(result)
