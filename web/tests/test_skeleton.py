"""Тесты каркаса: мастер первого запуска, вход и блокировка, роли и права, пользователи."""

import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, has_right
from core.models import AuditLog, ElectricityTariff, StudioSettings

PASSWORD = "Pech-2026-studio"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def make_user(username, *roles, password=PASSWORD):
    user = User.objects.create_user(username=username, password=password, first_name=username.title())
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def do_login(client, username, password=PASSWORD, ip="10.0.0.5", **extra):
    return client.post(
        reverse("login"), {"username": username, "password": password, **extra}, REMOTE_ADDR=ip
    )


# --- мастер первого запуска -------------------------------------------------------------------


@pytest.mark.django_db
def test_everything_redirects_to_setup_until_first_user(client):
    for url in ("/", "/login/", "/users/", "/settings/"):
        resp = client.get(url)
        assert resp.status_code == 302 and resp.url == reverse("setup"), url
    assert client.get("/healthz").status_code == 200


@pytest.mark.django_db
def test_setup_creates_admin_studio_and_tariff(client):
    resp = client.post(
        reverse("setup"),
        {
            "first_name": "Денис",
            "username": "denis",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "studio_name": "Моя студия",
            "timezone": "Europe/Moscow",
            "price_kwh": "7.45",
        },
    )
    assert resp.status_code == 302 and resp.url == reverse("dashboard")
    user = User.objects.get(username="denis")
    assert user.groups.filter(name=ROLE_ADMIN).exists()
    assert has_right(user, "users_manage")
    assert StudioSettings.load().name == "Моя студия"
    assert str(ElectricityTariff.on_date().price_kwh) == "7.45"
    assert AuditLog.objects.filter(action="setup").exists()
    # мастер больше недоступен
    assert client.get(reverse("setup")).status_code == 302
    # и пользователь уже вошёл
    assert client.get("/").status_code == 200


@pytest.mark.django_db
def test_setup_rejects_weak_password(client):
    resp = client.post(
        reverse("setup"),
        {
            "first_name": "Д",
            "username": "denis",
            "password1": "1111",
            "password2": "1111",
            "studio_name": "С",
            "timezone": "Europe/Moscow",
            "price_kwh": "7",
        },
    )
    assert resp.status_code == 200
    assert not User.objects.exists()


# --- вход -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_login_requires_auth_and_generic_error(client):
    make_user("anna", ROLE_MANAGER)
    resp = client.get("/")
    assert resp.status_code == 302 and resp.url.startswith(reverse("login"))

    wrong_pass = do_login(client, "anna", "nope")
    no_user = do_login(client, "ghost", "nope")
    assert "Неверный логин или пароль." in wrong_pass.content.decode()
    assert "Неверный логин или пароль." in no_user.content.decode()

    resp = do_login(client, "anna")
    assert resp.status_code == 302
    assert client.get("/").status_code == 200


@pytest.mark.django_db
@override_settings(LOGIN_MAX_ATTEMPTS=5, LOGIN_LOCK_MINUTES=5)
def test_ip_locked_after_5_failures(client):
    make_user("anna", ROLE_MANAGER)
    for _ in range(4):
        assert "Неверный логин" in do_login(client, "anna", "bad").content.decode()
    assert "закрыт на 5 мин" in do_login(client, "anna", "bad").content.decode()
    # даже верный пароль не пускает с заблокированного IP
    assert do_login(client, "anna").status_code == 200
    # а с другого IP — пускает
    assert do_login(client, "anna", ip="10.0.0.6").status_code == 302
    assert AuditLog.objects.filter(action="login_locked").count() == 1


@pytest.mark.django_db
def test_remember_me_sets_long_session(client, settings):
    make_user("anna", ROLE_MANAGER)
    do_login(client, "anna", remember="on")
    assert client.session.get_expiry_age() == settings.REMEMBER_DAYS * 86400
    assert not client.session.get_expire_at_browser_close()

    client.post(reverse("logout"))
    do_login(client, "anna")
    assert client.session.get_expire_at_browser_close()


@pytest.mark.django_db
def test_inactive_user_cannot_login(client):
    user = make_user("anna", ROLE_MANAGER)
    user.is_active = False
    user.save()
    assert do_login(client, "anna").status_code == 200


# --- роли и права -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_default_role_rights_match_tz():
    admin = make_user("denis", ROLE_ADMIN)
    manager = make_user("anna", ROLE_MANAGER)
    operator = make_user("sergey", ROLE_OPERATOR)
    both = make_user("igor", ROLE_MANAGER, ROLE_OPERATOR)

    assert has_right(admin, "printers_manage") and has_right(admin, "users_manage")
    assert has_right(manager, "orders_full") and not has_right(manager, "print_control")
    assert has_right(manager, "tariffs_view") and not has_right(manager, "tariffs_edit")
    assert has_right(operator, "print_control") and has_right(operator, "orders_anonymized")
    assert not has_right(operator, "orders_full") and not has_right(operator, "reports_cost")
    # права суммируются
    assert has_right(both, "orders_full") and has_right(both, "print_control")


@pytest.mark.django_db
def test_pages_closed_by_rights(client):
    make_user("denis", ROLE_ADMIN)
    make_user("anna", ROLE_MANAGER)
    make_user("sergey", ROLE_OPERATOR)

    do_login(client, "sergey")
    for url in ("/users/", "/roles/", "/journal/", "/settings/", "/backups/"):
        assert client.get(url).status_code == 403, url

    client.post(reverse("logout"))
    do_login(client, "anna")
    assert client.get("/users/").status_code == 403
    resp = client.get("/settings/")  # менеджер видит тарифы, но не меняет
    assert resp.status_code == 200 and "Добавить тариф" not in resp.content.decode()

    client.post(reverse("logout"))
    do_login(client, "denis")
    for url in ("/users/", "/roles/", "/journal/", "/settings/", "/backups/"):
        assert client.get(url).status_code == 200, url


# --- пользователи и роли ----------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_creates_user_with_two_roles(client):
    make_user("denis", ROLE_ADMIN)
    do_login(client, "denis")
    roles = Group.objects.filter(name__in=[ROLE_MANAGER, ROLE_OPERATOR])
    resp = client.post(
        reverse("user_create"),
        {
            "first_name": "Игорь",
            "username": "igor",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "roles": [g.pk for g in roles],
        },
    )
    assert resp.status_code == 302
    igor = User.objects.get(username="igor")
    assert set(igor.groups.values_list("name", flat=True)) == {ROLE_MANAGER, ROLE_OPERATOR}
    assert AuditLog.objects.filter(action="user_create", target="igor").exists()


@pytest.mark.django_db
def test_last_admin_cannot_be_demoted(client):
    admin = make_user("denis", ROLE_ADMIN)
    do_login(client, "denis")
    manager = Group.objects.get(name=ROLE_MANAGER)
    resp = client.post(
        reverse("user_edit", args=[admin.pk]),
        {"first_name": "Денис", "is_active": "on", "roles": [manager.pk]},
    )
    assert resp.status_code == 200
    assert admin.groups.filter(name=ROLE_ADMIN).exists()


@pytest.mark.django_db
def test_admin_resets_password(client):
    make_user("denis", ROLE_ADMIN)
    anna = make_user("anna", ROLE_MANAGER)
    do_login(client, "denis")
    new = "Novyi-parol-2026"
    resp = client.post(reverse("user_password", args=[anna.pk]), {"password1": new, "password2": new})
    assert resp.status_code == 302
    anna.refresh_from_db()
    assert anna.check_password(new)


@pytest.mark.django_db
def test_custom_role_and_admin_role_is_locked(client):
    make_user("denis", ROLE_ADMIN)
    do_login(client, "denis")
    resp = client.post(reverse("role_create"), {"name": "Стажёр", "rights": ["view_dashboard"]})
    assert resp.status_code == 302
    trainee = make_user("ivan", "Стажёр")
    assert has_right(trainee, "view_dashboard") and not has_right(trainee, "print_control")

    admin_group = Group.objects.get(name=ROLE_ADMIN)
    n = admin_group.permissions.count()
    client.post(reverse("role_edit", args=[admin_group.pk]), {"name": "X", "rights": []})
    admin_group.refresh_from_db()
    assert admin_group.name == ROLE_ADMIN and admin_group.permissions.count() == n


@pytest.mark.django_db
def test_backup_download_only_known_names(client, settings, tmp_path):
    settings.BACKUP_DIR = tmp_path
    (tmp_path / "studio3d_2026-10-27_03-30.dump").write_bytes(b"PGDMP")
    make_user("denis", ROLE_ADMIN)
    do_login(client, "denis")
    assert "studio3d_2026-10-27_03-30.dump" in client.get("/backups/").content.decode()
    ok = client.post(reverse("backup_download", args=["studio3d_2026-10-27_03-30.dump"]))
    assert ok.status_code == 200 and b"".join(ok.streaming_content) == b"PGDMP"
    assert client.post("/backups/..%2F.env").status_code == 404
