import asyncio
import time
from fractions import Fraction

import av
from aiortc import MediaStreamTrack

from .native import Pair


class Video(MediaStreamTrack):
    kind = "video"

    def __init__(self, pair: Pair, player: int):
        super().__init__()
        self.pair, self.player = pair, player
        self.started = time.monotonic()

    async def recv(self):
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        fd = self.pair.video_fds[self.player]
        loop.add_reader(fd, ready.set)
        try:
            await ready.wait()
        finally:
            loop.remove_reader(fd)
        frame = av.VideoFrame(240, 160, "rgba")
        frame.planes[0].update(self.pair.video(self.player))
        frame.pts = int((time.monotonic() - self.started) * 90000)
        frame.time_base = Fraction(1, 90000)
        return frame.reformat(format="yuv420p")


class Audio(MediaStreamTrack):
    kind = "audio"

    def __init__(self, pair: Pair, player: int):
        super().__init__()
        self.pair, self.player = pair, player
        self.pts = 0
        self.deadline = time.monotonic()

    async def recv(self):
        await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        count = self.pair.rate // 50
        now = time.monotonic()
        interval = count / self.pair.rate
        self.deadline += interval
        if self.deadline < now:
            self.deadline = now + interval
        frame = av.AudioFrame(format="s16", layout="stereo", samples=count)
        frame.planes[0].update(self.pair.audio(self.player, count))
        frame.sample_rate = self.pair.rate
        frame.time_base = Fraction(1, self.pair.rate)
        frame.pts = self.pts
        self.pts += count
        return frame
