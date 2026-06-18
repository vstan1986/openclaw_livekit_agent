"""
Diagnostic audio tap — peak-level monitoring of raw incoming audio.

Enabled via ``TAP_AUDIO=1`` (see ``my_agent.config``).
"""

from __future__ import annotations

import asyncio
import logging
import struct

from livekit import rtc

logger = logging.getLogger("agent.audio_diag")


async def tap_audio(track: rtc.Track, label: str) -> None:
    """Subscribe to a raw audio track and log peak levels for diagnostics.

    Logs every 25th frame at DEBUG level, plus a final INFO summary.
    """
    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
    count = 0
    sum_peak = 0
    max_peak = 0
    try:
        async for ev in stream:
            frame = ev.frame
            bytes_data = bytes(frame.data)
            n = len(bytes_data) // 2
            if n == 0:
                continue
            samples = struct.unpack(f"<{n}h", bytes_data[: n * 2])
            peak = max(abs(s) for s in samples)
            sum_peak += peak
            max_peak = max(max_peak, peak)
            count += 1
            if count == 1 or count % 25 == 0:
                avg = sum_peak / count
                logger.debug(
                    "[tap %s] frame #%d sr=%d ch=%d spc=%d "
                    "peak=%d avg_peak=%.0f max_peak=%d",
                    label, count, frame.sample_rate,
                    frame.num_channels, frame.samples_per_channel,
                    peak, avg, max_peak,
                )
    except Exception:
        logger.exception("[tap %s] stream error", label)
    finally:
        await stream.aclose()
        logger.info(
            "[tap %s] ended: frames=%d max_peak=%d",
            label, count, max_peak,
        )
