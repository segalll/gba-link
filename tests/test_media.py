import asyncio
import statistics
import time
from types import SimpleNamespace

import pytest

from gba_link import media
from gba_link.native import Link


async def test_audio_pitch_survives_hardware_rate_changes(test_rom):
    link = Link(test_rom, [None, None])
    tracks = [media.Audio(link, i) for i in range(2)]
    try:
        link.pause(False)
        for rate in (0, 1, 2, 3, 0):
            for i in range(2):
                link.keys(i, (rate + i) % 4)
            samples = [[], []]
            for n in range(20):
                frames = await asyncio.gather(*(track.recv() for track in tracks))
                if n >= 10:
                    for i, frame in enumerate(frames):
                        samples[i].extend(
                            memoryview(bytes(frame.planes[0])).cast("h")[::2]
                        )
            for waveform in samples:
                edges = [
                    i
                    for i in range(1, len(waveform))
                    if waveform[i - 1] < 5760 <= waveform[i]
                ]
                assert 23 <= len(edges) <= 28
                periods = [b - a for a, b in zip(edges, edges[1:])]
                assert link.rate / statistics.median(periods) == pytest.approx(
                    128, rel=0.02
                )
    finally:
        for track in tracks:
            track.stop()
        link.close()


async def test_audio_pacing_does_not_accumulate_scheduler_delay(test_rom, monkeypatch):
    interval = 0.02
    now = 0.0

    async def sleep(delay):
        nonlocal now
        now += delay + 0.001

    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(media, "asyncio", SimpleNamespace(sleep=sleep))
    link = Link(test_rom, [None, None])
    track = media.Audio(link, 0)
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
        link.close()


async def test_video_waits_for_new_frames_and_can_cancel_while_paused(test_rom):
    link = Link(test_rom, [None, None])
    track = media.Video(link, 0)
    pending = asyncio.create_task(track.recv())
    try:
        await asyncio.sleep(0.03)
        assert not pending.done()
        link.pause(False)
        frame = await asyncio.wait_for(pending, 1)
        assert (frame.width, frame.height) == (240, 160)
        link.pause(True)
        link.video(0)
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0.03)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        link.pause(False)
        await asyncio.wait_for(track.recv(), 1)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        track.stop()
        link.close()


@pytest.mark.parametrize("player_count", [2, 4])
async def test_linked_video_frames_arrive_together(test_rom, player_count):
    link = Link(test_rom, [None] * player_count)
    tracks = [media.Video(link, i) for i in range(player_count)]

    async def receive(track):
        times = []
        for _ in range(20):
            await track.recv()
            times.append(time.monotonic())
        return times

    try:
        link.pause(False)
        first, *others = await asyncio.wait_for(
            asyncio.gather(*(receive(track) for track in tracks)), 2
        )
        for times in others:
            assert statistics.median(abs(a - b) for a, b in zip(first, times)) < 0.008
    finally:
        for track in tracks:
            track.stop()
        link.close()
