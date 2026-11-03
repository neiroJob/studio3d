"""Operator notifications: access boundaries, durable events and delivery failures."""

import asyncio
import base64
import io
import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from asgiref.sync import async_to_sync
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.telegram_access import chat_access, issue_pairing, light_access, pair_chat
from core.telegram_bot import TelegramService, control_light, delivery_access, delivery_done
from core.telegram_events import record_finished, record_state
from core.telegram_forms import TelegramSettingsForm
from core.telegram_models import (
    TelegramDelivery,
    TelegramEvent,
    TelegramPairing,
    TelegramPrinterState,
    TelegramRecipient,
    TelegramRuntime,
    TelegramSettings,
)
from core.telegram_photos import (
    CameraPhoto,
    PhotoProvider,
    PhotoUnavailable,
    creality_answer,
    creality_offer,
    jpeg_bytes,
    webrtc_frame,
)
from printers import collector
from printers.drivers import base
from printers.models import Printer
from printers.state import write_state


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def setup_bot(db):
    user = User.objects.create_user("operator")
    user.groups.set(Group.objects.filter(name=ROLE_OPERATOR))
    printer = Printer.objects.create(name="Test Q1", profile_id="qidi_q1_pro", host="127.0.0.1", port=7125)
    recipient = TelegramRecipient.objects.create(name="Operator", kind="private", user=user, chat_id=123)
    config = TelegramSettings.load()
    config.enabled = True
    config.save()
    return user, printer, recipient, config


def state(status=base.PRINTING, **extra):
    return {
        **base.offline_state(updated=timezone.now().timestamp()),
        "status": status,
        "job": {"filename": "test.gcode", "progress": 0.25, "elapsed": 60, "remaining": 180},
        **extra,
    }


def finished(printer, **extra):
    return {
        "external_id": "test-job",
        "filename": "test.gcode",
        "status": "completed",
        "end_time": timezone.now().timestamp(),
        "print_duration": 240,
        **extra,
    }


def test_pairing_single_use_hash_and_expiry(setup_bot):
    _, _, recipient, _ = setup_bot
    recipient.chat_id = None
    recipient.save()
    token = issue_pairing(recipient)
    assert TelegramPairing.objects.get().token_hash != token
    with pytest.raises(ValidationError):
        pair_chat(token, -10, "group", 123)
    assert pair_chat(token, 456, "private", 456) == recipient.name
    with pytest.raises(ValidationError):
        pair_chat(token, 456, "private", 456)
    token = issue_pairing(recipient)
    TelegramPairing.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        pair_chat(token, 456, "private", 456)


def test_access_revocation_and_group_light_actor(setup_bot):
    user, printer, _, _ = setup_bot
    TelegramRecipient.objects.create(name="Workshop", kind="group", chat_id=-100)
    assert chat_access(-100, 999).kind == "group"
    with pytest.raises(PermissionDenied):
        light_access(-100, 999, printer.pk)
    assert light_access(-100, 123, printer.pk).pk == user.pk
    user.is_active = False
    user.save()
    with pytest.raises(PermissionDenied):
        chat_access(123, 123)
    with pytest.raises(PermissionDenied):
        light_access(-100, 123, printer.pk)


def test_scoped_light_and_no_other_controls(setup_bot):
    _, printer, recipient, _ = setup_bot
    other = Printer.objects.create(name="Other", profile_id="qidi_q1_pro", host="127.0.0.2", port=7125)
    recipient.printers.add(printer)
    with pytest.raises(PermissionDenied):
        control_light(123, 123, other.pk, "on")
    with pytest.raises(ValidationError):
        control_light(123, 123, printer.pk, "pause")
    with patch("core.telegram_bot.client_for") as driver:
        assert "включён" in control_light(123, 123, printer.pk, "on")
        driver.return_value.set_light.assert_called_once_with(True)
        assert driver.return_value.method_calls == [("set_light", (True,), {})]


def test_manager_cannot_control_light(setup_bot):
    user, printer, _, _ = setup_bot
    user.groups.set(Group.objects.filter(name=ROLE_MANAGER))
    with pytest.raises(PermissionDenied):
        light_access(123, 123, printer.pk)


def test_start_pause_resume_offline_dedup(setup_bot):
    _, printer, _, config = setup_bot
    config.offline_minutes = 0
    config.save()
    record_state(printer.pk, state())
    record_state(printer.pk, state())
    record_state(printer.pk, state(base.PAUSED))
    record_state(printer.pk, state())
    record_state(printer.pk, base.offline_state())
    record_state(printer.pk, base.offline_state())
    record_state(printer.pk, state())
    assert list(TelegramEvent.objects.values_list("kind", flat=True)) == [
        "start",
        "pause",
        "pause",
        "offline",
        "online",
    ]
    assert TelegramDelivery.objects.count() == 5


def test_periodic_and_removal_reminder(setup_bot):
    _, printer, _, _ = setup_bot
    record_state(printer.pk, state())
    TelegramPrinterState.objects.update(last_periodic=timezone.now() - timedelta(minutes=11))
    record_state(printer.pk, state())
    record_state(printer.pk, state())
    assert TelegramEvent.objects.filter(kind="periodic").count() == 1
    printer.awaiting_removal = True
    printer.save()
    record_state(printer.pk, state(base.READY))
    TelegramPrinterState.objects.update(last_reminder=timezone.now() - timedelta(minutes=31))
    record_state(printer.pk, state(base.READY))
    assert TelegramEvent.objects.filter(kind="reminder").count() == 1
    printer.awaiting_removal = False
    printer.save()
    record_state(printer.pk, state(base.READY))
    assert TelegramPrinterState.objects.get().last_reminder is None


def test_finish_ignores_imported_history_and_deduplicates(setup_bot):
    _, printer, _, _ = setup_bot
    record_finished(printer, finished(printer), live=False)
    assert not TelegramEvent.objects.exists()
    record_state(printer.pk, state())
    job = finished(printer)
    record_finished(printer, job, live=False)
    record_finished(printer, job, live=True)
    assert TelegramEvent.objects.filter(kind="finish").count() == 1
    assert TelegramEvent.objects.get(kind="finish").payload["status"] == base.FINISHED


def test_finish_catches_up_after_ready_arrives_before_history(setup_bot):
    _, printer, _, _ = setup_bot
    record_state(printer.pk, state())
    record_state(printer.pk, state(base.READY, job=None))
    assert not TelegramPrinterState.objects.get().active
    record_finished(printer, finished(printer), live=False)
    assert TelegramEvent.objects.filter(kind="finish").count() == 1


def test_disabled_and_recipient_scope(setup_bot):
    _, printer, recipient, config = setup_bot
    config.enabled = False
    config.save()
    record_state(printer.pk, state())
    assert not TelegramEvent.objects.exists()
    config.enabled = True
    config.destination = "group"
    config.save()
    record_state(printer.pk, state(base.PAUSED))
    assert TelegramEvent.objects.count() == 1
    assert not TelegramDelivery.objects.exists()
    assert recipient.enabled


def test_fault_is_sent_once(setup_bot):
    _, printer, _, _ = setup_bot
    fault = state(base.PAUSED, faults=["Закончилась нить"], filament={"present": False})
    write_state(printer.pk, fault)
    write_state(printer.pk, fault)
    assert TelegramEvent.objects.filter(kind="fault").count() == 1


def test_delivery_revokes_pending_and_retries(setup_bot):
    _, printer, recipient, _ = setup_bot
    record_state(printer.pk, state())
    delivery = TelegramDelivery.objects.get()
    assert delivery_access(delivery)[0] == 123
    delivery_done(delivery, "Ошибка", retry_after=45)
    delivery.refresh_from_db()
    assert delivery.attempts == 1 and delivery.sent_at is None
    assert delivery.next_attempt_at > timezone.now() + timedelta(seconds=40)
    recipient.enabled = False
    recipient.save()
    assert delivery_access(delivery) is None
    delivery.refresh_from_db()
    assert delivery.skipped


def test_admin_ui_permissions_and_interval_validation(setup_bot, client):
    user, _, _, config = setup_bot
    client.force_login(user)
    assert client.get(reverse("telegram_settings")).status_code == 403
    user.groups.set(Group.objects.filter(name=ROLE_ADMIN))
    response = client.get(reverse("telegram_settings"))
    assert response.status_code == 200
    assert "BotFather" in response.content.decode()
    assert "TELEGRAM_BOT_TOKEN" in response.content.decode()
    assert client.get(reverse("telegram_recipient_add")).status_code == 200
    form = TelegramSettingsForm(
        {"destination": "both", "interval_minutes": -1, "reminder_minutes": 0, "offline_minutes": 0},
        instance=config,
    )
    assert not form.is_valid() and "interval_minutes" in form.errors
    form = TelegramSettingsForm(
        {"destination": "both", "interval_minutes": 0, "reminder_minutes": 0, "offline_minutes": 0},
        instance=config,
    )
    assert form.is_valid(), form.errors


def test_admin_recipient_pair_test_and_unlink(setup_bot, client):
    user, _, _, _ = setup_bot
    user.groups.set(Group.objects.filter(name=ROLE_ADMIN))
    client.force_login(user)
    response = client.post(
        reverse("telegram_recipient_add"),
        {
            "name": "Group",
            "kind": "group",
            "enabled": "on",
            "subscribed": "on",
        },
    )
    assert response.status_code == 302
    recipient = TelegramRecipient.objects.get(name="Group")
    assert client.get(reverse("telegram_recipient_edit", args=[recipient.pk])).status_code == 200
    response = client.post(reverse("telegram_pairing", args=[recipient.pk]))
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    token = response.context["token"]
    pair_chat(token, -999, "supergroup", 123)
    assert client.post(reverse("telegram_test", args=[recipient.pk])).status_code == 302
    assert TelegramDelivery.objects.filter(recipient=recipient, event__kind="test").count() == 1
    assert client.post(reverse("telegram_unlink", args=[recipient.pk])).status_code == 302
    recipient.refresh_from_db()
    assert recipient.chat_id is None
    assert TelegramDelivery.objects.get(recipient=recipient).skipped


@pytest.mark.django_db(transaction=True)
def test_async_send_photo_fallback_and_retry(setup_bot):
    _, printer, _, _ = setup_bot
    record_state(printer.pk, state())
    delivery = TelegramDelivery.objects.select_related("event__printer", "recipient__user").get()
    bot = Mock(send_message=AsyncMock(), send_photo=AsyncMock())
    service = TelegramService(bot)
    service.photos.get = AsyncMock(side_effect=PhotoUnavailable("Нет кадра"))
    async_to_sync(service.deliver)(delivery)
    delivery.refresh_from_db()
    assert delivery.sent_at and "Фото:" in delivery.last_error
    assert "Нет кадра" in bot.send_message.call_args.args[1]
    bot.send_photo.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("error,disabled", [("retry", False), ("forbidden", True)])
def test_async_api_errors(setup_bot, error, disabled):
    _, printer, recipient, _ = setup_bot
    record_state(printer.pk, state())
    delivery = TelegramDelivery.objects.select_related("event__printer", "recipient__user").get()
    bot = Mock(send_message=AsyncMock(), send_photo=AsyncMock())
    method = SendMessage(chat_id=123, text="test")
    failure = (
        TelegramRetryAfter(method=method, message="rate", retry_after=42)
        if error == "retry"
        else TelegramForbiddenError(method=method, message="blocked")
    )
    bot.send_photo.side_effect = failure
    service = TelegramService(bot)
    service.photos.get = AsyncMock(return_value=CameraPhoto(b"jpeg", timezone.now()))
    async_to_sync(service.deliver)(delivery)
    delivery.refresh_from_db()
    recipient.refresh_from_db()
    assert delivery.sent_at is None and delivery.attempts == 1
    assert recipient.enabled is not disabled
    if disabled:
        assert delivery.skipped
    else:
        assert delivery.next_attempt_at > timezone.now() + timedelta(seconds=38)


@pytest.mark.django_db(transaction=True)
def test_forbidden_print_commands_never_reach_driver(setup_bot):
    bot = Mock(send_message=AsyncMock())
    service = TelegramService(bot)
    with patch("core.telegram_bot.client_for") as driver:
        for command in ("cancel", "pause", "resume", "start_print", "heat", "plate_cleared"):
            async_to_sync(service.command)(123, 123, command, ["1"])
        driver.assert_not_called()
    assert bot.send_message.await_count == 6


def test_jpeg_conversion_and_cache():
    source = io.BytesIO()
    Image.new("RGBA", (2400, 1200), (100, 80, 20, 255)).save(source, "PNG")
    converted = jpeg_bytes(source.getvalue())
    assert Image.open(io.BytesIO(converted)).size == (1920, 960)
    assert Image.open(io.BytesIO(converted)).format == "JPEG"
    printer = Mock(pk=1)
    printer.profile.camera_type = "mjpeg_snapshot"
    printer.profile.snapshot_url = "/snapshot"
    with patch("core.telegram_photos.fetch_snapshot", return_value=converted) as fetch:

        async def check():
            provider = PhotoProvider()
            first, second = await asyncio.gather(provider.get(printer), provider.get(printer))
            assert first is second

        asyncio.run(check())
        fetch.assert_called_once()


def test_runtime_offset_is_durable(db):
    runtime = TelegramRuntime.load()
    runtime.update_offset = 654
    runtime.save()
    assert TelegramRuntime.load().update_offset == 654


def test_history_survives_notification_failure(setup_bot):
    _, printer, _, _ = setup_bot
    job = finished(printer, start_time=1, total_duration=250, filament_mm=100, filament_g=2)
    with patch("core.telegram_events.record_finished", side_effect=RuntimeError("queue failed")):
        assert collector._save_job(printer.pk, job, True)
    printer.refresh_from_db()
    assert printer.awaiting_removal and printer.jobs.count() == 1
    assert printer.print_seconds == 240


def test_webrtc_offer_limits_candidates_to_routed_interface():
    sdp = (
        "v=0\r\nm=video 555 UDP/TLS/RTP/SAVPF 101\r\nc=IN IP4 10.0.0.2\r\n"
        "a=candidate:1 1 udp 1 10.0.0.2 555 typ host\r\n"
        "a=candidate:2 1 udp 1 192.0.2.2 666 typ host\r\n"
        "a=fingerprint:sha-256 AA\r\na=fingerprint:sha-512 BB\r\n"
    )
    with patch("core.telegram_photos.socket.socket") as sock:
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
        patch("core.telegram_photos.creality_offer", side_effect=lambda sdp, url: sdp),
        patch("core.telegram_photos.aiohttp.ClientSession", return_value=session_context),
    ):
        if valid:
            result = asyncio.run(webrtc_frame("http://192.0.2.10:8000/"))
            assert Image.open(io.BytesIO(result)).size == (64, 48)
        else:
            with pytest.raises(PhotoUnavailable):
                asyncio.run(webrtc_frame("http://192.0.2.10:8000/"))
    peer.close.assert_awaited_once()
