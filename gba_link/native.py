import ctypes
import os
import threading
from pathlib import Path


class Pair:
    def __init__(self, rom: Path, saves: list[Path | None]):
        self.lock = threading.Lock()
        self.lib = ctypes.CDLL(
            os.environ.get("GBA_LINK_LIBRARY", "build/libgba_link.so")
        )
        for name, args, result in (
            ("create", [ctypes.c_char_p] * 3, ctypes.c_void_p),
            ("destroy", [ctypes.c_void_p], None),
            ("pause", [ctypes.c_void_p, ctypes.c_int], None),
            ("keys", [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint16], None),
            ("video_fd", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            (
                "video",
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p],
                ctypes.c_uint64,
            ),
            (
                "audio",
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t],
                ctypes.c_size_t,
            ),
            ("audio_rate", [ctypes.c_void_p, ctypes.c_int], ctypes.c_uint),
            (
                "save",
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t],
                ctypes.c_size_t,
            ),
        ):
            function = getattr(self.lib, f"gba_link_{name}")
            function.argtypes, function.restype = args, result
        self.handle = self.lib.gba_link_create(
            os.fsencode(rom), *(os.fsencode(p) if p else None for p in saves)
        )
        if not self.handle:
            raise ValueError("Could not load GBA ROM or battery saves")
        self.rate = self.lib.gba_link_audio_rate(self.handle, 0)
        self.video_fds = [self.lib.gba_link_video_fd(self.handle, i) for i in range(2)]

    def close(self):
        with self.lock:
            self.lib.gba_link_destroy(self.handle)
            self.handle = None

    def pause(self, paused: bool):
        with self.lock:
            self.lib.gba_link_pause(self.handle, paused)

    def keys(self, player: int, keys: int):
        with self.lock:
            self.lib.gba_link_keys(self.handle, player, keys)

    def video(self, player: int) -> bytes:
        pixels = ctypes.create_string_buffer(240 * 160 * 4)
        with self.lock:
            self.lib.gba_link_video(self.handle, player, pixels)
        return pixels.raw

    def audio(self, player: int, frames: int) -> bytes:
        samples = ctypes.create_string_buffer(frames * 4)
        with self.lock:
            self.lib.gba_link_audio(self.handle, player, samples, frames)
        return samples.raw

    def save(self, player: int) -> bytes:
        data = ctypes.create_string_buffer(1024 * 1024)
        with self.lock:
            size = self.lib.gba_link_save(self.handle, player, data, len(data))
        return data.raw[:size]
