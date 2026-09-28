import asyncio
import contextlib
import io
import zipfile
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from av import AudioFrame, VideoFrame

from gba_link.native import Pair
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


async def test_auth_seats_and_stale_join(client, test_rom):
    assert (
        await client.post(f"{API}/activate", json=activation(test_rom))
    ).status == 401
    first = await activate(client, test_rom)
    assert "token=" in first["url"]
    payload = {"session_id": "old", "user": {"id": 2}}
    assert (await client.post(f"{API}/join", json=payload, headers=AUTH)).status == 409
    payload["session_id"] = "a1b2"
    second = await client.post(f"{API}/join", json=payload, headers=AUTH)
    assert second.status == 200
    room = await second.json()
    assert room["url"] != first["url"]
    again = await client.post(f"{API}/join", json=payload, headers=AUTH)
    assert (await again.json()) == room
    payload["user"] = {"id": 3}
    assert (await client.post(f"{API}/join", json=payload, headers=AUTH)).status == 409
    assert (await client.get("/gba-link/context")).status == 401


def archive(name="game.sav", content=b"\x42" * 32768):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(name, content)
    return buffer.getvalue()


async def test_separate_saves_survive_exit_and_new_session(client, test_rom):
    async def upload(content):
        response = await client.put(
            f"{API}/imports/save.zip", data=content, headers=AUTH
        )
        assert response.status == 200
        return (await response.json())["path"]

    left = await upload(archive(content=b"\x11" * 32768))
    right = await upload(archive(content=b"\x22" * 32768))
    assert left != right
    payload = activation(test_rom)
    payload["save"] = {"archive": left}
    assert (
        await client.post(f"{API}/activate", json=payload, headers=AUTH)
    ).status == 200
    payload = {"session_id": "a1b2", "user": {"id": 2}, "save": {"archive": right}}
    assert (await client.post(f"{API}/join", json=payload, headers=AUTH)).status == 200
    assert (
        await client.post(f"{API}/exit", json={"session_id": "a1b2"}, headers=AUTH)
    ).status == 200
    payload = activation(test_rom, "c3d4")
    assert (
        await client.post(f"{API}/activate", json=payload, headers=AUTH)
    ).status == 200
    for user_id, byte in ((1, 0x11), (2, 0x22)):
        path = f"{API}/exports/a1b2-{user_id}.zip"
        response = await client.get(path, headers=AUTH)
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(await response.read())) as zipped:
            assert zipped.read("game.sav") == bytes([byte]) * 32768
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


async def test_failed_join_closes_pair_and_leaves_second_seat_available(
    client, test_rom
):
    await activate(client, test_rom)
    service = client.server.app[SERVICE]
    payload = {"session_id": "a1b2", "user": {"id": 2}}
    with (
        patch.object(service, "write_roster", side_effect=OSError("disk full")),
        patch.object(Pair, "close", autospec=True, side_effect=Pair.close) as close,
    ):
        response = await client.post(f"{API}/join", json=payload, headers=AUTH)
        assert response.status == 500
        assert service.pair is None
        assert [seat.user_id for seat in service.seats] == [1]
        close.assert_called_once()

    response = await client.post(f"{API}/join", json=payload, headers=AUTH)
    assert response.status == 200
    response = await client.post(
        f"{API}/seal", json={"session_id": "a1b2"}, headers=AUTH
    )
    assert (await response.json()) == {"players": [1, 2]}


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


async def test_two_peers_receive_video_audio_and_pause_on_disconnect(client, test_rom):
    first = await activate(client, test_rom)
    response = await client.post(
        f"{API}/join", json={"session_id": "a1b2", "user": {"id": 2}}, headers=AUTH
    )
    second = await response.json()
    peers, senders, tracks = [], [], []

    async def connect(room, keys):
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
                if channel.readyState == "open":
                    channel.send(keys.to_bytes(2, "little"))
                await asyncio.sleep(0.05)

        senders.append(asyncio.create_task(inputs()))
        await peer.setLocalDescription(await peer.createOffer())
        response = await client.post(
            "/gba-link/offer",
            json={"sdp": peer.localDescription.sdp},
            headers={"Authorization": "Bearer " + room["url"].split("token=")[1]},
        )
        assert response.status == 200
        await peer.setRemoteDescription(RTCSessionDescription(**await response.json()))

    try:
        await connect(first, 0x1F)
        await connect(second, 0x3E0)
        # Keep reading so assertions use the latest frames.
        latest: list[dict[str, AudioFrame | VideoFrame]] = [{}, {}]
        changed = asyncio.Event()

        async def receive(i, kind):
            while True:
                frame = await tracks[i][kind].recv()
                assert isinstance(frame, (AudioFrame, VideoFrame))
                latest[i][kind] = frame
                changed.set()

        receivers = [
            asyncio.create_task(receive(i, kind))
            for i in range(2)
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
            assert service.pair.save(0)[0] == 0xE0
            assert service.pair.save(1)[0] == 0x1F
            await peers[1].close()
            await asyncio.sleep(1.25)
            assert not service.seats[1].ready
            before = service.pair.video(0)
            service.pair.keys(0, 0)
            await asyncio.sleep(0.1)
            assert service.pair.video(0) == before
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
