"""Native mobile API: bearer security, revocation and operator-only commands."""

import hashlib
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ROLE_MANAGER, ROLE_OPERATOR
from core.camera_photos import CameraPhoto, PhotoUnavailable
from core.mobile_models import MobileSession
from printers.drivers import base
from printers.models import Printer
from printers.state import write_state


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def mobile(db, client):
    user = User.objects.create_user("mobile-operator", password="Example-operator-2026")
    user.groups.set(Group.objects.filter(name=ROLE_OPERATOR))
    printer = Printer.objects.create(name="Mobile Q1", profile_id="qidi_q1_pro", host="127.0.0.1", port=7125)
    response = client.post(
        reverse("mobile_login"),
        {"username": user.username, "password": "Example-operator-2026", "device": "Test Android"},
        content_type="application/json",
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return user, printer, token, {"HTTP_AUTHORIZATION": "Bearer " + token}


def test_mobile_token_hash_and_no_cookie_access(mobile, client):
    user, _, token, headers = mobile
    assert MobileSession.objects.get().token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(list(MobileSession.objects.values()))
    client.force_login(user)
    assert client.get(reverse("mobile_fleet")).status_code == 401
    response = client.get(reverse("mobile_fleet"), **headers)
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert response.json()["printers"][0]["name"] == "Mobile Q1"


def test_fleet_contains_live_status_and_only_operator_fields(mobile, client):
    _, printer, _, headers = mobile
    write_state(
        printer.pk,
        {
            **base.offline_state(),
            "status": base.PRINTING,
            "filament": {"present": False, "enabled": True},
            "job": {"filename": "/private/customer/a.gcode", "progress": 0.5, "elapsed": 30, "remaining": 30},
        },
    )
    row = client.get(reverse("mobile_fleet"), **headers).json()["printers"][0]
    assert row["status"] == "printing" and row["job"]["filename"] == "a.gcode"
    assert row["filament"]["present"] is False and row["can_light"]
    assert "host" not in row and "api_key" not in row and "console" not in row


@pytest.mark.parametrize("mode", ["expired", "inactive", "permission"])
def test_mobile_access_revoked(mobile, client, mode):
    user, _, _, headers = mobile
    if mode == "expired":
        MobileSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    elif mode == "inactive":
        user.is_active = False
        user.save()
    else:
        user.groups.clear()
    assert client.get(reverse("mobile_fleet"), **headers).status_code in (401, 403)


def test_logout_revokes_token(mobile, client):
    _, _, _, headers = mobile
    assert (
        client.post(reverse("mobile_logout"), {}, content_type="application/json", **headers).status_code
        == 200
    )
    assert client.get(reverse("mobile_fleet"), **headers).status_code == 401


def test_password_change_revokes_token(mobile, client):
    user, _, _, headers = mobile
    user.set_password("New-example-2026-password")
    user.save()
    assert client.get(reverse("mobile_fleet"), **headers).status_code == 401
    assert not MobileSession.objects.exists()


def test_only_light_and_strict_boolean(mobile, client):
    _, printer, _, headers = mobile
    url = reverse("mobile_light", args=[printer.pk])
    with patch("core.mobile_api.client_for") as driver:
        assert (
            client.post(url, {"on": "false"}, content_type="application/json", **headers).status_code == 400
        )
        driver.assert_not_called()
        assert client.post(url, {"on": True}, content_type="application/json", **headers).status_code == 200
        driver.return_value.set_light.assert_called_once_with(True)
        assert len(driver.return_value.method_calls) == 1
    assert client.get(url, **headers).status_code == 405


def test_read_only_role_cannot_use_light(mobile, client):
    user, printer, _, headers = mobile
    user.groups.set(Group.objects.filter(name=ROLE_MANAGER))
    with patch("core.mobile_api.client_for") as driver:
        assert (
            client.post(
                reverse("mobile_light", args=[printer.pk]),
                {"on": True},
                content_type="application/json",
                **headers,
            ).status_code
            == 403
        )
        driver.assert_not_called()


def test_photo_success_and_explicit_failure(mobile, client):
    _, printer, _, headers = mobile
    url = reverse("mobile_photo", args=[printer.pk])

    async def frame(_):
        return CameraPhoto(b"test-jpeg", timezone.now())

    with patch("core.mobile_api.PhotoProvider") as provider:
        provider.return_value.get = frame
        response = client.get(url, **headers)
        assert response.status_code == 200 and response.content == b"test-jpeg"
        assert response["X-Captured-At"] and response["Cache-Control"] == "no-store"
    cache.clear()

    async def unavailable(_):
        raise PhotoUnavailable("Камера недоступна")

    with patch("core.mobile_api.PhotoProvider") as provider:
        provider.return_value.get = unavailable
        response = client.get(url, **headers)
        assert response.status_code == 503 and response.json()["error"] == "Камера недоступна"


def test_bad_login_json_and_lockout(mobile, client, settings):
    settings.LOGIN_MAX_ATTEMPTS = 2
    url = reverse("mobile_login")
    assert client.post(url, "[]", content_type="application/json").status_code == 400
    for _ in range(2):
        assert (
            client.post(
                url, {"username": "no-user", "password": "invalid"}, content_type="application/json"
            ).status_code
            == 401
        )
    assert client.post(url, {}, content_type="application/json").status_code == 429


def test_empty_history_events_and_no_print_routes(mobile, client):
    _, printer, _, headers = mobile
    assert client.get(reverse("mobile_history", args=[printer.pk]), **headers).json() == {"jobs": []}
    assert client.get(reverse("mobile_events"), **headers).status_code == 200
    assert client.get(reverse("mobile_history", args=[99999]), **headers).status_code == 404
    for command in ("pause", "resume", "cancel", "start", "heat"):
        assert (
            client.post(f"/api/mobile/v1/printers/{printer.pk}/{command}/", {}, **headers).status_code == 404
        )
