import asyncio
import contextlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from .media import Audio, Video
from .native import Pair

PREFIX = "/gba-link"
API = f"{PREFIX}/api/session"
MAX_SAVE = 1024 * 1024
STATIC = Path(__file__).with_name("static")
log = logging.getLogger(__name__)


@dataclass
class Seat:
    user_id: int
    save: Path | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    peer: RTCPeerConnection | None = None
    last_input: float = 0
    ready: bool = False


class Service:
    def __init__(self, data: Path, roms: Path, secret: str, ice_servers: list[dict]):
        if not secret:
            raise ValueError("BROKER_SECRET is required")
        self.data, self.roms, self.secret = data, roms.resolve(), secret
        self.ice_servers = ice_servers
        self.lock = asyncio.Lock()
        self.seats: list[Seat] = []
        self.pair: Pair | None = None
        self.session_id: str | None = None
        self.rom: Path | None = None
        self.sealed = False
        for name in ("imports", "exports", "sessions"):
            (data / name).mkdir(parents=True, exist_ok=True)

    def url(self, seat: Seat):
        # The URL fragment keeps the token out of access logs and Referer headers.
        return {"url": f"{PREFIX}/#token={seat.token}"}

    def import_save(self, payload: dict, user_id: int) -> Path | None:
        name = payload.get("save", {}).get("archive")
        if not name:
            return None
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}", name):
            raise web.HTTPBadRequest(text="Invalid save import")
        source = self.data / "imports" / name
        if not source.is_file():
            raise web.HTTPBadRequest(text="Save import expired")
        assert self.session_id is not None
        target = self.data / "sessions" / self.session_id / f"{user_id}.sav"
        shutil.copyfile(source, target)
        source.unlink()
        return target

    def authenticate(self, request: web.Request) -> tuple[int, Seat]:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        for i, seat in enumerate(self.seats):
            if hmac.compare_digest(token, seat.token):
                return i, seat
        raise web.HTTPUnauthorized()

    def seal(self):
        self.sealed = True
        return self.write_roster()

    def write_roster(self, players: list[int] | None = None):
        if players is None:
            players = [s.user_id for s in self.seats]
        target = self.data / "exports" / f"{self.session_id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(players))
        publish(temporary, target)
        return players

    async def readiness(self):
        if self.pair:
            ready = not self.sealed and all(
                s.ready and s.peer and s.peer.connectionState == "connected"
                for s in self.seats
            )
            await asyncio.to_thread(self.pair.pause, not ready)

    async def checkpoint(self):
        snapshots = []
        if self.pair:
            await asyncio.to_thread(self.pair.pause, True)
        try:
            for i, seat in enumerate(self.seats):
                content = (
                    await asyncio.to_thread(self.pair.save, i)
                    if self.pair
                    else (seat.save.read_bytes() if seat.save else b"")
                )
                if not content:
                    continue
                target = self.data / "exports" / f"{self.session_id}-{seat.user_id}.zip"
                snapshots.append((target, content))
        finally:
            await self.readiness()
        for target, content in snapshots:
            await asyncio.to_thread(write_save, target, content)

    async def close(self):
        if self.session_id:
            self.seal()
        await self.checkpoint()
        seats, self.seats = self.seats, []
        if self.pair:
            await asyncio.to_thread(self.pair.close)
            self.pair = None
        # Peer callbacks also take the service lock, which is held during close.
        for seat in seats:
            if seat.peer:
                seat.peer.remove_all_listeners()
                await seat.peer.close()
        if self.session_id:
            shutil.rmtree(self.data / "sessions" / self.session_id)
        self.session_id = None


SERVICE = web.AppKey("service", Service)


def write_save(target: Path, content: bytes):
    temporary = target.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("game.sav", content)
    publish(temporary, target)


def publish(temporary: Path, target: Path):
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(target)
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def user_id(payload: dict) -> int:
    value = payload.get("user", {}).get("id")
    if type(value) is not int or value <= 0:
        raise web.HTTPBadRequest(text="Invalid user")
    return value


@web.middleware
async def security(request, handler):
    service = request.app[SERVICE]
    if request.path.startswith(API):
        if not hmac.compare_digest(
            request.headers.get("X-Broker-Secret", ""), service.secret
        ):
            raise web.HTTPUnauthorized()
    try:
        response = await handler(request)
    except (ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        raise web.HTTPBadRequest(text="Invalid request") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; media-src 'self' blob:; style-src 'self'; script-src 'self'; base-uri 'none'"
    )
    return response


async def activate(request):
    service = request.app[SERVICE]
    payload = await request.json()
    sid = payload.get("session_id", "")
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{4,64}", sid):
        raise web.HTTPBadRequest(text="Invalid session")
    uid = user_id(payload)
    if (
        payload.get("emulator") != "mgba-link"
        or payload.get("rom", {}).get("platform") != "gba"
    ):
        raise web.HTTPBadRequest(text="A GBA ROM is required")
    rom = await asyncio.to_thread(Path(payload["rom"]["path"]).resolve)
    if (
        not rom.is_relative_to(service.roms)
        or not rom.is_file()
        or rom.suffix.lower() != ".gba"
        or rom.stat().st_size > 32 * 1024 * 1024
    ):
        raise web.HTTPBadRequest(text="ROM must be a .gba file within ROM_ROOT")
    async with service.lock:
        if service.session_id:
            if service.session_id == sid and service.seats[0].user_id == uid:
                return web.json_response(service.url(service.seats[0]))
            raise web.HTTPConflict(text="A room is already active")
        root = service.data / "sessions" / sid
        root.mkdir(exist_ok=False)
        service.session_id = sid
        service.sealed = False
        try:
            service.rom = root / "game.gba"
            await asyncio.to_thread(shutil.copyfile, rom, service.rom)
            seat = Seat(uid, service.import_save(payload, uid))
            service.seats = [seat]
            await asyncio.to_thread(service.write_roster)
        except Exception:
            service.session_id = None
            service.seats = []
            shutil.rmtree(root)
            raise
        return web.json_response(service.url(seat))


async def join(request):
    service = request.app[SERVICE]
    payload = await request.json()
    uid = user_id(payload)
    async with service.lock:
        if not service.session_id or payload.get("session_id") != service.session_id:
            raise web.HTTPConflict(text="The room has ended")
        if service.sealed:
            raise web.HTTPConflict(text="The room is closing")
        for seat in service.seats:
            if seat.user_id == uid:
                return web.json_response(service.url(seat))
        if len(service.seats) == 2:
            raise web.HTTPConflict(text="Both seats are taken")
        seat = Seat(uid, service.import_save(payload, uid))
        pair = await asyncio.to_thread(
            Pair, service.rom, [service.seats[0].save, seat.save]
        )
        try:
            await asyncio.to_thread(
                service.write_roster, [s.user_id for s in service.seats] + [uid]
            )
        except Exception:
            await asyncio.to_thread(pair.close)
            raise
        service.seats.append(seat)
        service.pair = pair
        return web.json_response(service.url(seat))


async def exit_session(request):
    service = request.app[SERVICE]
    payload = await request.json()
    sid = payload.get("session_id", "")
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{4,64}", sid):
        raise web.HTTPBadRequest(text="Invalid session")
    async with service.lock:
        if service.session_id == sid:
            await service.close()
        elif (
            service.session_id
            or not (service.data / "exports" / f"{sid}.json").is_file()
        ):
            raise web.HTTPConflict(text="The room has ended")
    return web.json_response({"status": "stopped", "state_saved": False})


async def seal(request):
    service = request.app[SERVICE]
    payload = await request.json()
    sid = payload.get("session_id", "")
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{4,64}", sid):
        raise web.HTTPBadRequest(text="Invalid session")
    async with service.lock:
        if sid == service.session_id:
            players = service.seal()
            await service.readiness()
        else:
            path = service.data / "exports" / f"{sid}.json"
            if not path.is_file():
                raise web.HTTPConflict(text="Unknown session")
            players = json.loads(path.read_text())
    return web.json_response({"players": players})


async def upload(request):
    service = request.app[SERVICE]
    content = await request.read()
    with zipfile.ZipFile(io.BytesIO(content)) as zipped:
        entries = zipped.infolist()
        if (
            len(entries) != 1
            or entries[0].filename != "game.sav"
            or not 0 < entries[0].file_size <= MAX_SAVE
        ):
            raise web.HTTPBadRequest(text="Expected one game.sav, at most 1 MiB")
        save = zipped.read(entries[0])
    name = secrets.token_hex(16)
    (service.data / "imports" / name).write_bytes(save)
    return web.json_response({"path": name})


async def export(request):
    service = request.app[SERVICE]
    name = request.match_info["name"]
    if not re.fullmatch(r"[0-9a-f]{4,64}-[1-9][0-9]*\.zip", name):
        raise web.HTTPNotFound()
    path = service.data / "exports" / name
    if not path.is_file():
        raise web.HTTPNotFound()
    if request.method == "DELETE":
        path.unlink()
        return web.json_response({"deleted": True})
    return web.Response(body=path.read_bytes(), content_type="application/zip")


async def context(request):
    service = request.app[SERVICE]
    player, _ = service.authenticate(request)
    return web.json_response(
        {
            "player": player + 1,
            "waiting": len(service.seats) < 2,
            "iceServers": service.ice_servers,
        }
    )


async def offer(request):
    service = request.app[SERVICE]
    payload = await request.json()
    async with service.lock:
        player, seat = service.authenticate(request)
        if not service.pair:
            raise web.HTTPConflict(text="Waiting for player two")
        pair = service.pair
        if seat.peer:
            seat.peer.remove_all_listeners()
            await seat.peer.close()
        seat.ready = False
        service.pair.keys(player, 0)
        peer = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[RTCIceServer(**item) for item in service.ice_servers]
            )
        )
        seat.peer = peer
        await service.readiness()

    @peer.on("connectionstatechange")
    async def state_changed():
        async with service.lock:
            if seat not in service.seats or seat.peer is not peer:
                return
            if peer.connectionState != "connected":
                seat.ready = False
                pair.keys(player, 0)
            await service.readiness()

    @peer.on("datachannel")
    def datachannel(channel):
        if channel.label != "inputs":
            channel.close()
            return

        @channel.on("message")
        def message(data):
            if seat.peer is not peer or seat not in service.seats:
                return
            if not isinstance(data, bytes) or len(data) != 2:
                return
            mask = int.from_bytes(data, "little")
            if mask & ~0x3FF:
                return
            seat.last_input = time.monotonic()
            seat.ready = True
            pair.keys(player, mask)

    try:
        await peer.setRemoteDescription(
            RTCSessionDescription(sdp=payload["sdp"], type="offer")
        )
        peer.addTrack(Video(pair, player))
        peer.addTrack(Audio(pair, player))
        await peer.setLocalDescription(await peer.createAnswer())
    except Exception:
        peer.remove_all_listeners()
        await peer.close()
        async with service.lock:
            if seat.peer is peer:
                seat.peer = None
                seat.ready = False
                pair.keys(player, 0)
                await service.readiness()
        raise
    return web.json_response(
        {"sdp": peer.localDescription.sdp, "type": peer.localDescription.type}
    )


async def static(request):
    name = request.match_info.get("name", "index.html")
    if name not in ("index.html", "client.js", "style.css"):
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name)


async def lifetime(app):
    service = app[SERVICE]

    async def maintain():
        checkpoint_at = time.monotonic() + 10
        while True:
            await asyncio.sleep(0.25)
            async with service.lock:
                for i, seat in enumerate(service.seats):
                    if seat.ready and time.monotonic() - seat.last_input > 1:
                        seat.ready = False
                        service.pair.keys(i, 0)
                await service.readiness()
                if time.monotonic() >= checkpoint_at:
                    try:
                        await service.checkpoint()
                    except OSError:
                        log.exception("Could not checkpoint battery saves")
                    checkpoint_at = time.monotonic() + 10

    task = asyncio.create_task(maintain())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    async with service.lock:
        await service.close()


def create_app(
    data: Path, roms: Path, secret: str, ice_servers: list[dict] | None = None
):
    app = web.Application(middlewares=[security], client_max_size=MAX_SAVE + 4096)
    app[SERVICE] = Service(data, roms, secret, ice_servers or [])
    app.cleanup_ctx.append(lifetime)
    app.add_routes(
        [
            web.post(f"{API}/activate", activate),
            web.post(f"{API}/join", join),
            web.post(f"{API}/exit", exit_session),
            web.post(f"{API}/seal", seal),
            web.put(f"{API}/imports/{{name}}", upload),
            web.get(f"{API}/exports/{{name}}", export),
            web.delete(f"{API}/exports/{{name}}", export),
            web.get(f"{PREFIX}/context", context),
            web.post(f"{PREFIX}/offer", offer),
            web.get(f"{PREFIX}/", static),
            web.get(f"{PREFIX}/{{name}}", static),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(
            Path(os.environ.get("DATA_ROOT", "/data")),
            Path(os.environ.get("ROM_ROOT", "/roms")),
            os.environ["BROKER_SECRET"],
            json.loads(os.environ.get("ICE_SERVERS", "[]")),
        ),
        # Bridged containers need all interfaces; host-network Compose uses loopback.
        host=os.environ.get("BIND_HOST", "0.0.0.0"),  # nosec B104
        port=int(os.environ.get("PORT", "8080")),
    )
