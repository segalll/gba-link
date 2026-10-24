import asyncio
import socket
from unittest.mock import patch

import pytest
from aiortc.sdp import SessionDescription

from gba_link.ice import add_forwarded_candidates

SDP = """v=0
o=- 0 0 IN IP4 172.18.0.2
s=-
t=0 0
m=video 50000 UDP/TLS/RTP/SAVPF 96
a=candidate:1 1 udp 2130706431 172.18.0.2 50000 typ host
a=candidate:2 1 udp 2130706431 fd00::2 50001 typ host
"""


async def test_forwarded_candidates_follow_dns_changes():
    with patch.object(asyncio.get_running_loop(), "getaddrinfo") as lookup:
        for public_ip in ("203.0.113.10", "203.0.113.11"):
            lookup.return_value = [
                (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (public_ip, 0)),
                (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("2001:db8::1", 0, 0, 0)),
            ]
            answer = await add_forwarded_candidates(
                SDP, ["link.example", "192.0.2.10", public_ip]
            )
            candidates = SessionDescription.parse(answer).media[0].ice_candidates
            forwarded = [c for c in candidates if c.type == "srflx"]
            assert len(forwarded) == 3
            assert {(c.ip, c.port, c.relatedAddress) for c in forwarded} == {
                (public_ip, 50000, "172.18.0.2"),
                ("192.0.2.10", 50000, "172.18.0.2"),
                ("2001:db8::1", 50001, "fd00::2"),
            }
        assert lookup.await_count == 2


@pytest.mark.parametrize("error", [socket.gaierror("No address"), TimeoutError()])
async def test_forwarded_candidates_fail_on_dns_error(error):
    with patch.object(asyncio.get_running_loop(), "getaddrinfo", side_effect=error):
        with pytest.raises(type(error)):
            await add_forwarded_candidates(SDP, ["link.example"])


async def test_stun_address_uses_forwarded_port():
    sdp = SDP + (
        "a=candidate:3 1 udp 1694498815 203.0.113.10 62000 typ srflx "
        "raddr 172.18.0.2 rport 50000\n"
    )
    answer = await add_forwarded_candidates(sdp, [])
    candidates = SessionDescription.parse(answer).media[0].ice_candidates
    assert {(c.ip, c.port) for c in candidates if c.type == "srflx"} == {
        ("203.0.113.10", 62000),
        ("203.0.113.10", 50000),
    }
    assert await add_forwarded_candidates(answer, []) == answer
