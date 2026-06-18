"""
Audio utilities for the STT microservice.

- _peak — max PCM16 amplitude in a chunk
- _apply_gain — multiply amplitude with saturation
- Voice/silence classification (threshold-based)
"""

from __future__ import annotations

import struct

# Threshold for "voice" classification
VOICE_THRESHOLD = 200


def peak(pcm16_bytes: bytes) -> int:
    """Maximum absolute PCM16 sample value in a chunk.

    - 0           = all zeros
    - 1-50        = very quiet (line noise floor)
    - 200+        = speech / phone comfort noise
    - 5000+       = loud speech
    - 32767       = clipping
    """
    if not pcm16_bytes:
        return 0
    n = len(pcm16_bytes) // 2
    try:
        samples = struct.unpack(f"<{n}h", pcm16_bytes[: n * 2])
    except struct.error:
        return 0
    return max(abs(s) for s in samples)


def apply_gain(pcm16_bytes: bytes, gain: float) -> bytes:
    """Multiply each PCM16 sample by gain with saturation in [-32768, 32767]."""
    if gain == 1.0 or not pcm16_bytes:
        return pcm16_bytes
    n = len(pcm16_bytes) // 2
    try:
        samples = struct.unpack(f"<{n}h", pcm16_bytes[: n * 2])
    except struct.error:
        return pcm16_bytes
    out = []
    for s in samples:
        v = int(s * gain)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.append(v)
    return struct.pack(f"<{n}h", *out)
