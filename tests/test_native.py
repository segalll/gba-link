import ctypes
import struct
import time

import pytest

from gba_link.native import Pair


@pytest.fixture
def pair(test_rom):
    pair = Pair(test_rom, [None, None])
    yield pair
    pair.close()


def video(pair, player):
    pixels = ctypes.create_string_buffer(240 * 160 * 4)
    frame = pair.lib.gba_link_video(pair.handle, player, pixels)
    return frame, struct.unpack_from("<3I", pixels)


def rgb555(value):
    components = [(value >> shift) & 31 for shift in (0, 5, 10)]
    return (
        sum(((v << 3) | (v >> 2)) << (8 * i) for i, v in enumerate(components))
        | 0xFF000000
    )


def test_link_transfers_each_players_input(pair):
    pair.keys(0, 1)
    pair.keys(1, 2)
    pair.pause(False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        left = video(pair, 0)
        right = video(pair, 1)
        if left[1][1:] == right[1][1:] == (rgb555(0x3FE), rgb555(0x3FD)):
            break
        time.sleep(0.02)
    assert left[0] > 0 and right[0] > 0
    assert left[1] == (rgb555(0x3FE), rgb555(0x3FE), rgb555(0x3FD))
    assert right[1] == (rgb555(0x3FD), rgb555(0x3FE), rgb555(0x3FD))


def test_linked_cores_run_at_gba_speed(pair):
    started = time.monotonic()
    pair.pause(False)
    time.sleep(0.6)
    pair.pause(True)
    elapsed = time.monotonic() - started
    for player in range(2):
        assert 45 < video(pair, player)[0] / elapsed < 75


def test_pause_and_independent_battery_saves(pair, test_rom, tmp_path):
    pair.keys(0, 1)
    pair.keys(1, 2)
    pair.pause(False)
    time.sleep(0.15)
    pair.pause(True)
    before = [video(pair, i)[0] for i in range(2)]
    time.sleep(0.05)
    assert [video(pair, i)[0] for i in range(2)] == before
    paths = []
    for player, expected in enumerate((0xFE, 0xFD)):
        data = pair.save(player)
        assert len(data) == 32768
        assert data[0] == expected
        path = tmp_path / f"{player}.sav"
        path.write_bytes(data)
        paths.append(path)
    restored = Pair(test_rom, paths)
    try:
        for player in range(2):
            assert restored.save(player) == paths[player].read_bytes()
    finally:
        restored.close()


def test_invalid_rom_does_not_start(tmp_path):
    path = tmp_path / "bad.gba"
    path.write_bytes(b"not a ROM")
    with pytest.raises(ValueError):
        Pair(path, [None, None])
