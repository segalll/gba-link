import ctypes
import struct
import time

import pytest

from gba_link.native import Link


@pytest.fixture(params=[2, 3, 4])
def player_count(request):
    return request.param


@pytest.fixture
def link(test_rom, player_count):
    link = Link(test_rom, [None] * player_count)
    yield link
    link.close()


def video(link, player):
    pixels = ctypes.create_string_buffer(240 * 160 * 4)
    frame = link.lib.gba_link_video(link.handle, player, pixels)
    return frame, struct.unpack_from("<5I", pixels)


def rgb555(value):
    components = [(value >> shift) & 31 for shift in (0, 5, 10)]
    return (
        sum(((v << 3) | (v >> 2)) << (8 * i) for i, v in enumerate(components))
        | 0xFF000000
    )


def test_link_transfers_each_players_input(link, player_count):
    count = player_count
    for i in range(count):
        link.keys(i, 1 << i)
    colors = [rgb555(v) for v in (0x3FE, 0x3FD, 0x3FB, 0x3F7)[:count]]
    received = tuple(colors + [rgb555(0xFFFF)] * (4 - count))
    link.pause(False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        frames = [video(link, i) for i in range(count)]
        if all(pixels[1:] == received for _, pixels in frames):
            break
        time.sleep(0.02)
    for (frame, pixels), color in zip(frames, colors):
        assert frame > 0
        assert pixels == (color, *received)


def test_linked_cores_run_at_gba_speed(link):
    started = time.monotonic()
    link.pause(False)
    time.sleep(0.6)
    link.pause(True)
    elapsed = time.monotonic() - started
    for player in range(len(link.video_fds)):
        assert 45 < video(link, player)[0] / elapsed < 75


def test_pause_and_independent_battery_saves(link, test_rom, tmp_path):
    count = len(link.video_fds)
    for player in range(count):
        link.keys(player, 1 << player)
    link.pause(False)
    time.sleep(0.15)
    link.pause(True)
    before = [video(link, i)[0] for i in range(count)]
    time.sleep(0.05)
    assert [video(link, i)[0] for i in range(count)] == before
    paths = []
    for player, expected in enumerate((0xFE, 0xFD, 0xFB, 0xF7)[:count]):
        data = link.save(player)
        assert len(data) == 32768
        assert data[0] == expected
        path = tmp_path / f"{player}.sav"
        path.write_bytes(data)
        paths.append(path)
    restored = Link(test_rom, paths)
    try:
        for player in range(count):
            assert restored.save(player) == paths[player].read_bytes()
    finally:
        restored.close()


def test_invalid_rom_does_not_start(tmp_path):
    path = tmp_path / "bad.gba"
    path.write_bytes(b"not a ROM")
    with pytest.raises(ValueError):
        Link(path, [None, None])
