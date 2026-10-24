import asyncio
import socket
from dataclasses import replace
from ipaddress import ip_address

from aioice.candidate import candidate_foundation, candidate_priority
from aiortc.sdp import SessionDescription


async def add_forwarded_candidates(sdp: str, addresses: list[str]) -> str:
    resolved = {}
    async with asyncio.timeout(5):
        for address in addresses:
            try:
                resolved[str(ip_address(address))] = None
            except ValueError:
                records = await asyncio.get_running_loop().getaddrinfo(
                    address, None, type=socket.SOCK_DGRAM
                )
                resolved.update((record[4][0], None) for record in records)
    description = SessionDescription.parse(sdp)
    resolved.update(
        (candidate.ip, None)
        for media in description.media
        for candidate in media.ice_candidates
        if candidate.type == "srflx"
    )
    for media in description.media:
        for candidate in media.ice_candidates[:]:
            if candidate.type != "host" or candidate.protocol != "udp":
                continue
            for address in resolved:
                if ip_address(address).version != ip_address(candidate.ip).version:
                    continue
                forwarded = replace(
                    candidate,
                    foundation=candidate_foundation("srflx", "udp", candidate.ip),
                    priority=candidate_priority(candidate.component, "srflx"),
                    ip=address,
                    type="srflx",
                    relatedAddress=candidate.ip,
                    relatedPort=candidate.port,
                )
                if forwarded not in media.ice_candidates:
                    media.ice_candidates.append(forwarded)
    return str(description)
