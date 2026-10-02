import asyncio
import time
from fractions import Fraction

import av
from aiortc import MediaStreamTrack

from .native import Link


class Video(MediaStreamTrack):
    kind = "video"

    def __init__(self, link: Link, player: int):
        super().__init__()
        self.link, self.player = link, player
        self.started = time.monotonic()

    async def recv(self):
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        fd = self.link.video_fds[self.player]
        loop.add_reader(fd, ready.set)
        try:
            await ready.wait()
        finally:
            loop.remove_reader(fd)
        frame = av.VideoFrame(240, 160, "rgba")
        frame.planes[0].update(self.link.video(self.player))
        frame.pts = int((time.monotonic() - self.started) * 90000)
        frame.time_base = Fraction(1, 90000)
        return frame.reformat(format="yuv420p")


class Audio(MediaStreamTrack):
    kind = "audio"

    def __init__(self, link: Link, player: int):
        super().__init__()
        self.link, self.player = link, player
        self.pts = 0
        self.deadline = time.monotonic()

    async def recv(self):
        await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        count = self.link.rate // 50
        now = time.monotonic()
        interval = count / self.link.rate
        self.deadline += interval
        if self.deadline < now:
            self.deadline = now + interval
        frame = av.AudioFrame(format="s16", layout="stereo", samples=count)
        frame.planes[0].update(self.link.audio(self.player, count))
        frame.sample_rate = self.link.rate
        frame.time_base = Fraction(1, self.link.rate)
        frame.pts = self.pts
        self.pts += count
        return frame
