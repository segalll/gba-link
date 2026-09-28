from types import SimpleNamespace

import pytest

from gba_link import media
from gba_link.native import Pair


@pytest.mark.parametrize(
    "track_type, interval",
    [(media.Video, 280896 / 16777216), (media.Audio, 655 / 32768)],
)
async def test_media_pacing_does_not_accumulate_scheduler_delay(
    test_rom, monkeypatch, track_type, interval
):
    now = 0.0

    async def sleep(delay):
        nonlocal now
        now += delay + 0.001

    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(media, "asyncio", SimpleNamespace(sleep=sleep))
    pair = Pair(test_rom, [None, None])
    track = track_type(pair, 0)
    try:
        for _ in range(50):
            await track.recv()
            now += 0.004
        assert now == pytest.approx(49 * interval, abs=0.006)
        now += 1
        await track.recv()
        resumed = now
        await track.recv()
        assert now - resumed == pytest.approx(interval, abs=0.002)
    finally:
        track.stop()
        pair.close()
