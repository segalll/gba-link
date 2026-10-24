from dataclasses import replace
from ipaddress import ip_address

from aioice.candidate import candidate_foundation, candidate_priority
from aiortc.sdp import SessionDescription


def add_forwarded_candidates(sdp: str, addresses: list[str]) -> str:
    if not addresses:
        return sdp
    description = SessionDescription.parse(sdp)
    for media in description.media:
        for candidate in media.ice_candidates[:]:
            if candidate.type != "host" or candidate.protocol != "udp":
                continue
            for address in addresses:
                if ip_address(address).version != ip_address(candidate.ip).version:
                    continue
                media.ice_candidates.append(
                    replace(
                        candidate,
                        foundation=candidate_foundation("srflx", "udp", candidate.ip),
                        priority=candidate_priority(candidate.component, "srflx"),
                        ip=address,
                        type="srflx",
                        relatedAddress=candidate.ip,
                        relatedPort=candidate.port,
                    )
                )
    return str(description)
