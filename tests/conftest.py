import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gba_link.server import create_app


@pytest.fixture(scope="session")
def test_rom(tmp_path_factory):
    root = tmp_path_factory.mktemp("rom")
    obj = root / "test.o"
    rom = root / "test.gba"
    subprocess.run(
        [
            "clang",
            "--target=armv4t-none-eabi",
            "-c",
            str(Path(__file__).with_name("link_test.s")),
            "-o",
            str(obj),
        ],
        check=True,
    )
    subprocess.run(
        ["objcopy", "-I", "elf32-little", "-O", "binary", str(obj), str(rom)],
        check=True,
    )
    data = bytearray(rom.read_bytes())
    data[0xA0:0xAC] = b"ROMM LINK   "
    data[0xB2] = 0x96
    rom.write_bytes(data)
    return rom


@pytest.fixture
async def client(tmp_path, test_rom, request):
    app = create_app(
        tmp_path / "data",
        test_rom.parent,
        "test-secret",
        player_count=getattr(request, "param", 2),
    )
    async with TestClient(TestServer(app)) as client:
        yield client
