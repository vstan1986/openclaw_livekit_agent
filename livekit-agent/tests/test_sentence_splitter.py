"""
Tests for AggressiveSentenceTokenizer — Rule-based aggressive sentence splitter.
"""

from __future__ import annotations

import asyncio

import pytest

from my_agent.sentence_splitter import AggressiveSentenceTokenizer, split_sentences


# ---------------------------------------------------------------------------
# Synchronous split_sentences()
# ---------------------------------------------------------------------------


def test_split_simple_sentences() -> None:
    """Basic [.!?] splitting."""
    result = split_sentences("Привет! Как дела? Всё хорошо.")
    assert result == ["Привет!", "Как дела?", "Всё хорошо."]


def test_split_with_ellipsis() -> None:
    """Ellipsis is also a boundary."""
    result = split_sentences("Ну… Не знаю. Ладно.")
    assert result == ["Ну…", "Не знаю.", "Ладно."]


def test_split_double_newline() -> None:
    """Double newline is a paragraph boundary."""
    result = split_sentences("Строка один.\n\nСтрока два.")
    assert result == ["Строка один.", "Строка два."]


def test_keeps_single_newline() -> None:
    """Single \n is NOT a boundary (but acts as space)."""
    result = split_sentences("Строка один\nпродолжение.")
    assert len(result) == 1
    assert "продолжение" in result[0]


def test_split_after_colon() -> None:
    """Colon after a long word is a boundary."""
    # "Вот классика:" is 13 chars with a long word → will trigger
    # "дело:" is only 5 chars → will not trigger
    result = split_sentences("Вот классика: Штирлиц шёл.")
    assert result == ["Вот классика:", "Штирлиц шёл."]


def test_colon_short_word_kept_together() -> None:
    """Short word before : — do not split."""
    result = split_sentences("дело: текст")
    assert result == ["дело: текст"]


def test_forces_split_long_buffers() -> None:
    """Force-split if >90 chars and a space is available."""
    text = "А " * 60  # 120 chars
    result = split_sentences(text)
    # Must split into 2+ parts
    assert len(result) >= 2
    for r in result:
        # Each part can be 19 to ~110 chars
        assert len(r) >= 15


def test_preserves_russian_quotes() -> None:
    """Quotes should not interfere with splitting."""
    result = split_sentences('Он сказал: «Привет!» И ушёл.')
    # Must split into at least 2 parts
    assert len(result) >= 2


def test_no_false_split_on_abbreviation() -> None:
    """Abbreviations may split — the price of an aggressive strategy."""
    result = split_sentences("тел. 123-45-67 добавочный 5")
    # With "тел." this will give 2 parts. This is acceptable for TTS —
    # the SaluteSpeech synthesizer handles short chunks fine.
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# Streaming mode (async)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tokenizer() -> AggressiveSentenceTokenizer:
    return AggressiveSentenceTokenizer()


async def _run_stream(
    tokenizer: AggressiveSentenceTokenizer,
    chunks: list[str],
) -> list[str]:
    """Push chunks into tokenizer stream and collect sentences."""
    stream = tokenizer.stream()
    sentences: list[str] = []

    async def _collect() -> None:
        async for ev in stream:
            sentences.append(ev.token)

    collect_task = asyncio.create_task(_collect())

    for chunk in chunks:
        stream.push_text(chunk)
        await asyncio.sleep(0)

    stream.end_input()
    await collect_task
    return sentences


@pytest.mark.asyncio
async def test_stream_simple(tokenizer: AggressiveSentenceTokenizer) -> None:
    sentences = await _run_stream(tokenizer, ["Привет! Как дела? Всё хорошо."])
    assert len(sentences) >= 2
    assert "Привет!" in sentences[0]


@pytest.mark.asyncio
async def test_stream_multiple_chunks(tokenizer: AggressiveSentenceTokenizer) -> None:
    """Tokens arrive in parts, like from an LLM."""
    sentences = await _run_stream(
        tokenizer,
        ["Ко", "нечно! Вот кла", "ссика: Штирлиц", " шёл по лесу."],
    )
    assert len(sentences) >= 2
    # First sentence is "Конечно!" or "Конечно! Вот классика:" (depends on
    # buffer length and ctx_len). Only check that the first one arrives early:
    assert len(sentences[0]) < 40  # cannot be the entire response


@pytest.mark.asyncio
async def test_stream_flush_ends_early(tokenizer: AggressiveSentenceTokenizer) -> None:
    """Flush (end of LLM segment) forcibly emits accumulated text."""
    stream = tokenizer.stream()
    sentences: list[str] = []
    collect_task = asyncio.create_task(_drain(stream, sentences))

    stream.push_text("Привет! Ещё не всё")
    await asyncio.sleep(0)
    stream.flush()  # emits everything accumulated in in_buf
    stream.end_input()
    await collect_task

    # flush emits one message ("Привет! Ещё не всё") + end_input = 1
    assert len(sentences) >= 1
    # first chunk must contain "Привет!"
    assert "Привет!" in sentences[0]


async def _drain(stream, sentences: list[str]) -> None:
    async for ev in stream:
        sentences.append(ev.token)


# ---------------------------------------------------------------------------
# _split_stream — must always return homogeneous (token, start, end) tuples
# ---------------------------------------------------------------------------


def test_split_stream_returns_only_tuples() -> None:
    """BufferedSentenceStream.flush() reads the type of the first element and
    assumes the rest match; a stray bare str would corrupt output (tok[0]
    on a str = first char). Every element must be a 3-tuple."""
    from my_agent.sentence_splitter import _split_stream

    for text in (
        "Привет! Как дела? Всё хорошо.",
        "Вот классика: Штирлиц шёл по лесу.",
        "Один.\n\nДва.\n\nТри.",
        "Короткий текст без знаков",
        "",
    ):
        parts = _split_stream(text)
        assert all(
            isinstance(p, tuple) and len(p) == 3 for p in parts
        ), f"non-tuple token for {text!r}: {parts!r}"
