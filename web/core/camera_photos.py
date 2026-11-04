"""Свежие JPEG для мобильного API, включая нативные WebRTC-камеры Creality."""

import asyncio
import base64
import io
import json
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
from django.conf import settings
from django.utils import timezone
from PIL import Image

from printers.drivers.moonraker import fetch_snapshot


class PhotoUnavailable(Exception):
    pass


@dataclass
class CameraPhoto:
    data: bytes
    captured_at: object


def jpeg_bytes(data):
    with Image.open(io.BytesIO(data)) as image:
        image.thumbnail((1920, 1920))
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=85)
        return output.getvalue()


async def webrtc_frame(page_url):
    # Протокол взят из HTML /:8000 самого принтера: base64 JSON offer/answer.
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    stun = getattr(settings, "CAMERA_STUN", "")
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=stun)] if stun else []))
    future = asyncio.get_running_loop().create_future()
    tasks = []

    async def receive(track):
        try:
            frame = await track.recv()
            if not future.done():
                future.set_result(frame)
        except Exception as error:
            if not future.done():
                future.set_exception(error)

    @peer.on("track")
    def on_track(track):
        if track.kind == "video":
            tasks.append(asyncio.create_task(receive(track)))

    try:
        peer.addTransceiver("video", direction="sendrecv")
        await peer.setLocalDescription(await peer.createOffer())
        offer_sdp = creality_offer(peer.localDescription.sdp, page_url)
        offer = base64.b64encode(json.dumps({"type": "offer", "sdp": offer_sdp}).encode())
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(
                urljoin(page_url.rstrip("/") + "/", "call/webrtc_local"),
                data=offer,
                headers={"Content-Type": "plain/text"},
            ) as response:
                response.raise_for_status()
                answer = json.loads(base64.b64decode(await response.read(), validate=True))
        if answer.get("type") != "answer" or not isinstance(answer.get("sdp"), str):
            raise PhotoUnavailable("Камера не приняла подключение")
        await peer.setRemoteDescription(
            RTCSessionDescription(sdp=creality_answer(answer["sdp"]), type="answer")
        )
        frame = await future
        image = frame.to_image()
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        return output.getvalue()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(peer.close(), 5)


def creality_offer(sdp, page_url):
    """Send the printer only the routed LAN candidate and SHA-256 fingerprint."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((urlsplit(page_url).hostname, 8000))
        address = route.getsockname()[0]
    lines = sdp.replace("\r\n", "\n").splitlines()
    candidates = [line for line in lines if line.startswith("a=candidate:") and line.split()[4] == address]
    if not candidates:
        return sdp
    port = candidates[0].split()[5]
    output = []
    for line in lines:
        if line.startswith("a=candidate:") and line not in candidates:
            continue
        if line.startswith("a=fingerprint:") and not line.startswith("a=fingerprint:sha-256 "):
            continue
        if line.startswith("c=IN IP4 "):
            line = f"c=IN IP4 {address}"
        if line.startswith("m=video "):
            parts = line.split()
            parts[1] = port
            line = " ".join(parts)
        output.append(line)
    return "\r\n".join(output) + "\r\n"


def creality_answer(sdp):
    """K1C 2025 advertises H264 PT 0/96, but transmits PT 98.

    Match this specific firmware signature only. aiortc's H264 depayloader handles
    single NAL and FU-A packets; register both actual dynamic payload types.
    See ha_creality_ws camera.py, _note_failed_snapshot (upstream issue #46).
    """
    lines = sdp.replace("\r\n", "\n").splitlines()
    if "a=rtpmap:0 H264/90000" not in lines or "a=rtpmap:96 H264/90000" not in lines:
        return sdp
    output = []
    for line in lines:
        if line.startswith("m=video "):
            output.append(" ".join(line.split()[:3]) + " 96 98")
        elif line.startswith(("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:")):
            continue
        else:
            output.append(line)
    for payload in (96, 98):
        output.extend(
            [
                f"a=rtpmap:{payload} H264/90000",
                f"a=fmtp:{payload} level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
            ]
        )
    return "\r\n".join(output) + "\r\n"


class PhotoProvider:
    def __init__(self):
        self.locks = {}
        self.recent = {}
        self.slots = asyncio.Semaphore(2)

    async def get(self, printer):
        lock = self.locks.setdefault(printer.pk, asyncio.Lock())
        async with lock:
            cached = self.recent.get(printer.pk)
            if cached and (timezone.now() - cached.captured_at).total_seconds() < 10:
                return cached
            profile = printer.profile
            if profile is None:
                raise PhotoUnavailable("Профиль камеры не настроен")
            try:
                async with self.slots:
                    if profile.camera_type == "mjpeg_snapshot" and profile.snapshot_url:
                        data = await asyncio.to_thread(
                            fetch_snapshot, profile.snapshot_url_for(printer.host), 5
                        )
                    elif profile.camera_type == "webrtc_iframe" and profile.page_url:
                        data = await asyncio.wait_for(webrtc_frame(profile.page_url_for(printer.host)), 35)
                    else:
                        raise PhotoUnavailable("У этого принтера не настроена камера")
                    if not data:
                        raise PhotoUnavailable("Камера не отвечает")
                    data = await asyncio.to_thread(jpeg_bytes, data)
                photo = CameraPhoto(data, timezone.now())
                self.recent[printer.pk] = photo
                return photo
            except PhotoUnavailable:
                raise
            except Exception as error:
                # Не передавать в API адреса/ответы устройства или внутренний стек.
                raise PhotoUnavailable("Не удалось получить кадр камеры") from error
