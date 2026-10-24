import asyncio
import contextlib
import io
import zipfile
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceGatherer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import SessionDescription
from av import AudioFrame, VideoFrame

from gba_link.native import Link
from gba_link.server import SERVICE, create_app

AUTH = {"X-Broker-Secret": "test-secret"}
API = "/gba-link/api/session"


def activation(test_rom, session="a1b2"):
    return {
        "session_id": session,
        "user": {"id": 1},
        "emulator": "mgba-link",
        "rom": {"id": 42, "path": str(test_rom), "platform": "gba"},
    }


async def activate(client, test_rom):
    response = await client.post(
        f"{API}/activate", json=activation(test_rom), headers=AUTH
    )
    assert response.status == 200
    return await response.json()


@pytest.mark.parametrize("client", [2, 3, 4], indirect=True)
async def test_auth_seats_and_stale_join(client, test_rom):
    assert (
        await client.post(f"{API}/activate", json=activation(test_rom))
    ).status == 401
    first = await activate(client, test_rom)
    assert "token=" in first["url"]
    payload = {"session_id": "old", "user": {"id": 2}}
    assert (await client.post(f"{API}/join", json=payload, headers=AUTH)).status == 409
    payload["session_id"] = "a1b2"
    count = client.server.app[SERVICE].player_count
    token = {"Authorization": "Bearer " + first["url"].split("token=")[1]}
    for uid in range(2, count + 1):
        context = await client.get("/gba-link/context", headers=token)
        assert (await context.json())["waiting"]
        response = await client.post("/gba-link/offer", json={}, headers=token)
        assert response.status == 409
        payload["user"] = {"id": uid}
        response = await client.post(f"{API}/join", json=payload, headers=AUTH)
        assert response.status == 200
        room = await response.json()
        assert room["url"] != first["url"]
    context = await client.get("/gba-link/context", headers=token)
    assert not (await context.json())["waiting"]
    again = await client.post(f"{API}/join", json=payload, headers=AUTH)
    assert (await again.json()) == room
    payload["user"] = {"id": count + 1}
    assert (await client.post(f"{API}/join", json=payload, headers=AUTH)).status == 409
    assert (await client.get("/gba-link/context")).status == 401


def archive(name="game.sav", content=b"\x42" * 32768):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize("client", [2, 4], indirect=True)
async def test_separate_saves_survive_exit_and_new_session(client, test_rom):
    async def upload(content):
        response = await client.put(
            f"{API}/imports/save.zip", data=content, headers=AUTH
        )
        assert response.status == 200
        return (await response.json())["path"]

    count = client.server.app[SERVICE].player_count
    for uid in range(1, count + 1):
        save = await upload(archive(content=bytes([uid * 0x11]) * 32768))
        payload = {
            **activation(test_rom),
            "user": {"id": uid},
            "save": {"archive": save},
        }
        action = "activate" if uid == 1 else "join"
        assert (
            await client.post(f"{API}/{action}", json=payload, headers=AUTH)
        ).status == 200
    pending = await upload(archive())
    for uid, action in ((1, "activate"), (count, "join")):
        extra = await upload(archive(content=b"\xff" * 32768))
        payload = {
            **activation(test_rom),
            "user": {"id": uid},
            "save": {"archive": extra},
        }
        assert (
            await client.post(f"{API}/{action}", json=payload, headers=AUTH)
        ).status == 200
        imports = client.server.app[SERVICE].data / "imports"
        assert {p.name for p in imports.iterdir()} == {pending}
    response = await client.post(
        f"{API}/seal", json={"session_id": "a1b2"}, headers=AUTH
    )
    assert (await response.json()) == {"players": list(range(1, count + 1))}
    assert (
        await client.post(f"{API}/exit", json={"session_id": "a1b2"}, headers=AUTH)
    ).status == 200
    payload = activation(test_rom, "c3d4")
    assert (
        await client.post(f"{API}/activate", json=payload, headers=AUTH)
    ).status == 200
    for user_id in range(1, count + 1):
        path = f"{API}/exports/a1b2-{user_id}.zip"
        response = await client.get(path, headers=AUTH)
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(await response.read())) as zipped:
            assert zipped.read("game.sav") == bytes([user_id * 0x11]) * 32768
        assert (await client.get(path, headers=AUTH)).status == 200
        assert (await client.delete(path, headers=AUTH)).status == 200
        assert (await client.get(path, headers=AUTH)).status == 404


async def test_reject_unsafe_import_and_rom(client, test_rom, tmp_path):
    for name in ("../game.sav", "/game.sav", "game.exe"):
        response = await client.put(
            f"{API}/imports/save.zip", data=archive(name), headers=AUTH
        )
        assert response.status == 400
    payload = activation(tmp_path / "outside.gba")
    assert (
        await client.post(f"{API}/activate", json=payload, headers=AUTH)
    ).status == 400
    payload = activation(test_rom)
    payload["save"] = {"archive": "../../etc/passwd"}
    assert (
        await client.post(f"{API}/activate", json=payload, headers=AUTH)
    ).status == 400


async def test_seal_prevents_join_racing_exit(client, test_rom):
    await activate(client, test_rom)
    payload = {"session_id": "a1b2"}
    response = await client.post(f"{API}/seal", json=payload, headers=AUTH)
    assert (await response.json()) == {"players": [1]}
    response = await client.post(
        f"{API}/join", json={**payload, "user": {"id": 2}}, headers=AUTH
    )
    assert response.status == 409
    await client.post(f"{API}/exit", json=payload, headers=AUTH)
    response = await client.post(f"{API}/seal", json=payload, headers=AUTH)
    assert (await response.json()) == {"players": [1]}


@pytest.mark.parametrize(
    "client, joining", [(2, 2), (4, 2), (4, 4)], indirect=["client"]
)
async def test_failed_join_leaves_seat_available(client, test_rom, joining):
    await activate(client, test_rom)
    service = client.server.app[SERVICE]
    for uid in range(2, joining):
        response = await client.post(
            f"{API}/join",
            json={"session_id": "a1b2", "user": {"id": uid}},
            headers=AUTH,
        )
        assert response.status == 200
    payload = {"session_id": "a1b2", "user": {"id": joining}}
    with (
        patch.object(service, "write_roster", side_effect=OSError("disk full")),
        patch.object(Link, "close", autospec=True, side_effect=Link.close) as close,
    ):
        response = await client.post(f"{API}/join", json=payload, headers=AUTH)
        assert response.status == 500
        assert service.link is None
        assert [seat.user_id for seat in service.seats] == list(range(1, joining))
        assert close.call_count == (joining == service.player_count)

    response = await client.post(f"{API}/join", json=payload, headers=AUTH)
    assert response.status == 200
    response = await client.post(
        f"{API}/seal", json={"session_id": "a1b2"}, headers=AUTH
    )
    assert (await response.json()) == {"players": list(range(1, joining + 1))}


async def test_old_exit_cannot_stop_a_replacement_room(client, test_rom):
    await activate(client, test_rom)
    assert (
        await client.post(f"{API}/exit", json={"session_id": "a1b2"}, headers=AUTH)
    ).status == 200
    assert (
        await client.post(
            f"{API}/activate", json=activation(test_rom, "c3d4"), headers=AUTH
        )
    ).status == 200
    assert (
        await client.post(f"{API}/exit", json={"session_id": "a1b2"}, headers=AUTH)
    ).status == 409


async def test_slow_peer_negotiation_does_not_block_sealing(client, test_rom):
    first = await activate(client, test_rom)
    await client.post(
        f"{API}/join", json={"session_id": "a1b2", "user": {"id": 2}}, headers=AUTH
    )
    negotiating, release = asyncio.Event(), asyncio.Event()

    async def slow_offer(*args):
        negotiating.set()
        await release.wait()
        raise ValueError("Invalid test offer")

    with patch.object(RTCPeerConnection, "setRemoteDescription", slow_offer):
        pending = asyncio.create_task(
            client.post(
                "/gba-link/offer",
                json={"sdp": ""},
                headers={"Authorization": "Bearer " + first["url"].split("token=")[1]},
            )
        )
        try:
            await asyncio.wait_for(negotiating.wait(), 1)
            response = await asyncio.wait_for(
                client.post(f"{API}/seal", json={"session_id": "a1b2"}, headers=AUTH), 1
            )
            assert response.status == 200
        finally:
            release.set()
            await pending


async def test_roster_survives_service_restart(client, test_rom):
    await activate(client, test_rom)
    await client.post(
        f"{API}/join", json={"session_id": "a1b2", "user": {"id": 2}}, headers=AUTH
    )
    service = client.server.app[SERVICE]
    app = create_app(service.data, service.roms, "test-secret")
    async with TestClient(TestServer(app)) as restarted:
        response = await restarted.post(
            f"{API}/seal", json={"session_id": "a1b2"}, headers=AUTH
        )
        assert response.status == 200
        assert (await response.json()) == {"players": [1, 2]}
        response = await restarted.post(
            f"{API}/exit", json={"session_id": "a1b2"}, headers=AUTH
        )
        assert response.status == 200


@pytest.mark.parametrize("action", ["exit", "replace"])
async def test_pending_offer_is_closed_before_exit_or_replacement(
    client, test_rom, action
):
    first = await activate(client, test_rom)
    await client.post(
        f"{API}/join", json={"session_id": "a1b2", "user": {"id": 2}}, headers=AUTH
    )
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("video", direction="recvonly")
    browser.addTransceiver("audio", direction="recvonly")
    browser.createDataChannel("inputs")
    await browser.setLocalDescription(await browser.createOffer())
    headers = {"Authorization": "Bearer " + first["url"].split("token=")[1]}
    payload = {"sdp": browser.localDescription.sdp}
    started, resume = asyncio.Event(), asyncio.Event()
    gather = RTCIceGatherer.gather
    transports = {}
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))

    async def delayed(self):
        started.set()
        await resume.wait()
        await gather(self)
        transports[self] = [p.transport for p in self._connection._protocols]

    requests = []
    try:
        with patch.object(RTCIceGatherer, "gather", delayed):
            requests.append(
                asyncio.create_task(
                    client.post("/gba-link/offer", json=payload, headers=headers)
                )
            )
            await asyncio.wait_for(started.wait(), 1)
            peer = client.server.app[SERVICE].seats[0].peer
            gatherer = peer.getTransceivers()[
                0
            ].receiver.transport.transport.iceGatherer
            request = (
                client.post(f"{API}/exit", json={"session_id": "a1b2"}, headers=AUTH)
                if action == "exit"
                else client.post("/gba-link/offer", json=payload, headers=headers)
            )
            requests.append(asyncio.create_task(request))
            await asyncio.sleep(0.05)
            assert peer.connectionState != "closed"
            resume.set()
            stale, current = await asyncio.wait_for(asyncio.gather(*requests), 2)
            assert (stale.status, current.status) == (409, 200)
            assert peer.connectionState == "closed"
            assert transports[gatherer]
            assert all(t.is_closing() for t in transports[gatherer])
            assert not errors
    finally:
        resume.set()
        await asyncio.gather(*requests, return_exceptions=True)
        await browser.close()
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize(
    "client, disconnect",
    [(2, "peer"), (2, "channel"), (4, "peer"), (4, "channel")],
    indirect=["client"],
)
async def test_peers_receive_video_audio_and_pause_on_disconnect(
    client, test_rom, disconnect
):
    count = client.server.app[SERVICE].player_count
    rooms = [await activate(client, test_rom)]
    for uid in range(2, count + 1):
        response = await client.post(
            f"{API}/join",
            json={"session_id": "a1b2", "user": {"id": uid}},
            headers=AUTH,
        )
        assert response.status == 200
        rooms.append(await response.json())
    peers, senders, tracks = [], [], []
    keys = [0x1F, 0x3E0, 0x155, 0x2AA][:count]
    expected = [0xE0, 0x1F, 0xAA, 0x55][:count]

    async def connect(room, player):
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers.append(peer)
        media: dict[str, MediaStreamTrack] = {}
        tracks.append(media)

        @peer.on("track")
        def on_track(track):
            media[track.kind] = track

        peer.addTransceiver("video", direction="recvonly")
        peer.addTransceiver("audio", direction="recvonly")
        channel = peer.createDataChannel("inputs", ordered=False, maxRetransmits=0)

        async def inputs():
            while True:
                if channel.readyState == "open" and keys[player] is not None:
                    channel.send(keys[player].to_bytes(2, "little"))
                await asyncio.sleep(0.05)

        senders.append(asyncio.create_task(inputs()))
        await peer.setLocalDescription(await peer.createOffer())
        response = await client.post(
            "/gba-link/offer",
            json={"sdp": peer.localDescription.sdp},
            headers={"Authorization": "Bearer " + room["url"].split("token=")[1]},
        )
        assert response.status == 200
        answer = await response.json()
        for section in SessionDescription.parse(answer["sdp"]).media:
            hosts = [
                c
                for c in section.ice_candidates
                if c.type == "host" and ":" not in c.ip
            ]
            for host in hosts:
                forwarded = [
                    c
                    for c in section.ice_candidates
                    if c.type == "srflx"
                    and c.relatedAddress == host.ip
                    and c.relatedPort == host.port
                ]
                assert {c.ip for c in forwarded} == {"203.0.113.10", "192.0.2.10"}
                assert all(
                    c.port == host.port and c.priority < host.priority
                    for c in forwarded
                )
        await peer.setRemoteDescription(RTCSessionDescription(**answer))
        return channel

    try:
        for i, room in enumerate(rooms):
            last_channel = await connect(room, i)
        # Keep reading so assertions use the latest frames.
        latest: list[dict[str, AudioFrame | VideoFrame]] = [{} for _ in rooms]
        changed = asyncio.Event()

        async def receive(i, kind):
            while True:
                frame = await tracks[i][kind].recv()
                assert isinstance(frame, (AudioFrame, VideoFrame))
                latest[i][kind] = frame
                changed.set()

        receivers = [
            asyncio.create_task(receive(i, kind))
            for i in range(count)
            for kind in ("audio", "video")
        ]
        try:
            async with asyncio.timeout(10):
                while any(
                    "audio" not in frames or "video" not in frames for frames in latest
                ) or not all(s.ready for s in client.server.app[SERVICE].seats):
                    changed.clear()
                    await changed.wait()
                await asyncio.sleep(0.5)
            assert all(p.connectionState == "connected" for p in peers)
            assert all(
                isinstance(f["video"], VideoFrame)
                and f["video"].width == 240
                and f["video"].height == 160
                for f in latest
            )
            assert all(
                isinstance(f["audio"], AudioFrame) and f["audio"].sample_rate == 48000
                for f in latest
            )
            for frames in latest:
                samples = memoryview(bytes(frames["audio"].planes[0])).cast("h")
                assert max(samples) - min(samples) > 1000
            colors = [bytes(f["video"].planes[0])[20:120] for f in latest]
            assert abs(sum(colors[0]) - sum(colors[1])) > 1000
            service = client.server.app[SERVICE]
            assert [service.link.save(i)[0] for i in range(count)] == expected
            last_keys, keys[-1] = keys[-1], None
            await asyncio.sleep(1.5)
            before = [frames["video"].pts for frames in latest]
            keys[0] = 1
            await asyncio.sleep(0.2)
            assert all(frames["video"].pts > pts for frames, pts in zip(latest, before))
            assert service.link.save(0)[0] == 0xFE
            assert service.link.save(count - 1)[0] == 0xFF
            keys[-1] = last_keys
            await asyncio.sleep(0.2)
            assert service.link.save(count - 1)[0] == expected[-1]
            if disconnect == "peer":
                await peers[-1].close()
            else:
                last_channel.close()
            await asyncio.sleep(1.25)
            if disconnect == "channel":
                assert peers[-1].connectionState == "connected"
            assert not service.seats[-1].ready
            before = service.link.video(0)
            service.link.keys(0, 0)
            await asyncio.sleep(0.1)
            assert service.link.video(0) == before
        finally:
            for task in receivers:
                task.cancel()
            await asyncio.gather(*receivers, return_exceptions=True)
    finally:
        for task in senders:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for peer in peers:
            await peer.close()
