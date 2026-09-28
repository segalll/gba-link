import asyncio
import statistics
import time
from types import SimpleNamespace

import pytest

from gba_link import media
from gba_link.native import Pair


async def test_audio_pacing_does_not_accumulate_scheduler_delay(test_rom, monkeypatch):
    interval = 655 / 32768
    now = 0.0

    async def sleep(delay):
        nonlocal now
        now += delay + 0.001

    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(media, "asyncio", SimpleNamespace(sleep=sleep))
    pair = Pair(test_rom, [None, None])
    track = media.Audio(pair, 0)
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


async def test_video_waits_for_new_frames_and_can_cancel_while_paused(test_rom):
    pair = Pair(test_rom, [None, None])
    track = media.Video(pair, 0)
    pending = asyncio.create_task(track.recv())
    try:
        await asyncio.sleep(0.03)
        assert not pending.done()
        pair.pause(False)
        frame = await asyncio.wait_for(pending, 1)
        assert (frame.width, frame.height) == (240, 160)
        pair.pause(True)
        pair.video(0)
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0.03)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        pair.pause(False)
        await asyncio.wait_for(track.recv(), 1)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        track.stop()
        pair.close()


async def test_linked_video_frames_arrive_together(test_rom):
    pair = Pair(test_rom, [None, None])
    tracks = [media.Video(pair, i) for i in range(2)]

    async def receive(track):
        times = []
        for _ in range(20):
            await track.recv()
            times.append(time.monotonic())
        return times

    try:
        pair.pause(False)
        left, right = await asyncio.wait_for(
            asyncio.gather(*(receive(track) for track in tracks)), 2
        )
        assert statistics.median(abs(a - b) for a, b in zip(left, right)) < 0.008
    finally:
        for track in tracks:
            track.stop()
        pair.close()
