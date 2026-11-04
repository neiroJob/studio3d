"""Shared camera capture: JPEG conversion and native WebRTC compatibility."""

import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from PIL import Image

from core.camera_photos import (
    PhotoProvider,
    PhotoUnavailable,
    creality_answer,
    creality_offer,
    jpeg_bytes,
    webrtc_frame,
)


def test_jpeg_conversion_and_cache():
    source = io.BytesIO()
    Image.new("RGBA", (2400, 1200), (100, 80, 20, 255)).save(source, "PNG")
    converted = jpeg_bytes(source.getvalue())
    assert Image.open(io.BytesIO(converted)).size == (1920, 960)
    assert Image.open(io.BytesIO(converted)).format == "JPEG"
    printer = Mock(pk=1)
    printer.profile.camera_type = "mjpeg_snapshot"
    printer.profile.snapshot_url = "/snapshot"
    with patch("core.camera_photos.fetch_snapshot", return_value=converted) as fetch:

        async def check():
            provider = PhotoProvider()
            first, second = await asyncio.gather(provider.get(printer), provider.get(printer))
            assert first is second

        asyncio.run(check())
        fetch.assert_called_once()


def test_webrtc_offer_limits_candidates_to_routed_interface():
    sdp = (
        "v=0\r\nm=video 555 UDP/TLS/RTP/SAVPF 101\r\nc=IN IP4 10.0.0.2\r\n"
        "a=candidate:1 1 udp 1 10.0.0.2 555 typ host\r\n"
        "a=candidate:2 1 udp 1 192.0.2.2 666 typ host\r\n"
        "a=fingerprint:sha-256 AA\r\na=fingerprint:sha-512 BB\r\n"
    )
    with patch("core.camera_photos.socket.socket") as sock:
        sock.return_value.__enter__.return_value.getsockname.return_value = ("192.0.2.2", 1234)
        offer = creality_offer(sdp, "http://192.0.2.10:8000/")
    assert "m=video 666 " in offer and "c=IN IP4 192.0.2.2" in offer
    assert "10.0.0.2" not in offer and "sha-512" not in offer


def test_webrtc_firmware_payload_compatibility_is_narrow():
    normal = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 101\r\na=rtpmap:101 H264/90000\r\n"
    assert creality_answer(normal) == normal
    firmware = (
        "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96 0\r\na=rtpmap:0 H264/90000\r\n"
        "a=rtpmap:96 H264/90000\r\na=fmtp:0 packetization-mode=1\r\n"
        "a=fingerprint:sha-256 AA\r\na=setup:passive\r\n"
    )
    answer = creality_answer(firmware)
    assert "a=rtpmap:98 H264/90000" in answer
    assert "a=rtpmap:0 " not in answer
    assert "a=fingerprint:sha-256 AA" in answer


@pytest.mark.parametrize("valid", [True, False])
def test_webrtc_frame_and_connection_cleanup(valid):
    peer = Mock()
    peer.createOffer = AsyncMock()
    peer.setLocalDescription = AsyncMock()
    peer.localDescription.sdp = "v=0\r\n"
    peer.close = AsyncMock()
    handlers = {}

    def on(name):
        def register(callback):
            handlers[name] = callback
            return callback

        return register

    peer.on = on
    track = Mock(kind="video")
    frame = Mock()
    frame.to_image.return_value = Image.new("RGB", (64, 48), "green")
    track.recv = AsyncMock(return_value=frame)

    async def remote(_):
        handlers["track"](track)

    peer.setRemoteDescription = AsyncMock(side_effect=remote)
    response = Mock()
    response.read = AsyncMock(
        return_value=base64.b64encode(
            json.dumps(
                {
                    "type": "answer" if valid else "offer",
                    "sdp": "v=0\r\n",
                }
            ).encode()
        )
    )
    request = AsyncMock()
    request.__aenter__.return_value = response
    session = Mock(post=Mock(return_value=request))
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    with (
        patch("aiortc.RTCPeerConnection", return_value=peer),
        patch("core.camera_photos.creality_offer", side_effect=lambda sdp, url: sdp),
        patch("core.camera_photos.aiohttp.ClientSession", return_value=session_context),
    ):
        if valid:
            result = asyncio.run(webrtc_frame("http://192.0.2.10:8000/"))
            assert Image.open(io.BytesIO(result)).size == (64, 48)
        else:
            with pytest.raises(PhotoUnavailable):
                asyncio.run(webrtc_frame("http://192.0.2.10:8000/"))
    peer.close.assert_awaited_once()
