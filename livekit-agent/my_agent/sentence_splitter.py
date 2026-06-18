"""
Aggressive sentence tokenizer for real-time TTS streaming.

Alternative to blingfire.SentenceTokenizer: splits on minimal sentence-end
signals (*without* look-ahead) so the first audio from Sber arrives as
early as possible.

Split rules (in priority order):
  1. [.!?…]+  followed by whitespace or end-of-string.
  2. \n\n      — double newline (LLM paragraph).
  3. :  after ≥3 words (→ "Here's the thing:").
  4. ; / —     after ≥3 words.
  5. Force: ~100 chars → split at the last space.
"""

from __future__ import annotations

import functools
import re
from typing import ClassVar

from livekit.agents.tokenize import SentenceTokenizer, SentenceStream
from livekit.agents.tokenize.token_stream import BufferedSentenceStream

_SEP = re.compile(
    r"(?<=[.!?…])"
    r"(?=\s|$)"
    r"|"
    r"\n\n+"
)
"""Main regex splitter: any match is a sentence boundary."""


def _has_colon_or_semicolon_break(text: str) -> int | None:
    """Check if we can split on : ; — after a long word (≥6 chars).

    Returns the position AFTER the character (so the character stays
    in the first part).
    """
    m = re.search(r"[\w]{6,}([:;—])(?=\s)", text.strip())
    if m:
        return m.end()  # include the character
    return None


def _hard_split_hint(text: str) -> int | None:
    """Force-split a long buffer at the nearest space."""
    if len(text) <= 90:
        return None
    # Find the last space in range [60, 100)
    idx = text.rfind(" ", 60, 100)
    if idx > 0:
        return idx + 1  # after the space
    # Not found — try any space after position 60
    idx = text.find(" ", 60)
    if idx > 0:
        return idx + 1
    return None


def _post_split_parts(parts: list[str]) -> list[str]:
    """Further split parts on :;— and force-split by length."""
    result: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Split on : ; — after a long word
        while (cut := _has_colon_or_semicolon_break(p)) is not None:
            result.append(p[:cut].strip())
            p = p[cut:].strip()
        # Force-split long chunks
        while (cut := _hard_split_hint(p)) is not None:
            result.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            result.append(p)
    return result


def split_sentences(text: str, *, language: str | None = None) -> list[str]:
    """Split text into sentences (synchronous mode).

    Returns the smallest possible chunks — no merging.
    Merging happens in BufferedSentenceStream (via min_token_len).
    """
    if not text.strip():
        return _post_split_parts([text])

    raw = _SEP.split(text)
    raw = [p.strip() for p in raw if p.strip()]
    return _post_split_parts(raw)


def _split_stream(text: str) -> list[tuple[str, int, int]]:
    """Function for BufferedSentenceStream — returns (token, start, end) tuples.

    Always returns tuples (even in fallback when the exact position could not
    be found) so that BufferedSentenceStream.flush() never receives a mixed
    list of str/tuple — otherwise it would infer the type from only the first
    element and return just the first char (tok[0]) for a string after a tuple.
    """
    parts = split_sentences(text)
    # Restore positions in the original text (str.find)
    result: list[tuple[str, int, int]] = []
    pos = 0
    for part in parts:
        idx = text.find(part, pos)
        if idx < 0:
            # Fallback — exact position not found, use approximate
            # but still a tuple (homogeneous list).
            result.append((part, pos, pos + len(part)))
            pos += len(part)
            continue
        result.append((part, idx, idx + len(part)))
        pos = idx + len(part)
    return result


class AggressiveSentenceTokenizer(SentenceTokenizer):
    """SentenceTokenizer with aggressive early-exit for the first sentence.

    Parameters:
        min_sentence_len: Minimum chunk length before sending to TTS.
                          If the sentence is shorter — wait for the next one.
        stream_context_len: How many characters of context to accumulate
                            before the first attempt to split the buffer.
    """

    DEFAULT_MIN_SENTENCE_LEN: ClassVar[int] = 15
    DEFAULT_STREAM_CONTEXT_LEN: ClassVar[int] = 10

    def __init__(
        self,
        *,
        min_sentence_len: int = DEFAULT_MIN_SENTENCE_LEN,
        stream_context_len: int = DEFAULT_STREAM_CONTEXT_LEN,
    ) -> None:
        self._min_sentence_len = min_sentence_len
        self._stream_context_len = stream_context_len

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        return split_sentences(text, language=language)

    def stream(self, *, language: str | None = None) -> SentenceStream:
        return BufferedSentenceStream(
            tokenizer=functools.partial(_split_stream),
            min_token_len=self._min_sentence_len,
            min_ctx_len=self._stream_context_len,
        )
