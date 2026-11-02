"""Тесты CRM: карточки клиента и заказа (ROADMAP.md, «CRM»)."""

import tempfile
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.models import (
    AuditLog,
    Client,
    ContactPerson,
    Order,
    OrderChecklistItem,
    OrderEvent,
    OrderFile,
    OrderItem,
    OrderStatus,
)

PASSWORD = "Pech-2026-studio"


def make_user(username, *roles):
    user = User.objects.create_user(username=username, password=PASSWORD, first_name=username)
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def login(client, username):
    assert client.post(reverse("login"), {"username": username, "password": PASSWORD}).status_code == 302


def make_client(name="ООО Вектор"):
    return Client.objects.create(name=name, kind=Client.Kind.COMPANY, inn="7701234567")


def make_order(client_obj=None, price="10000", **extra):
    client_obj = client_obj or make_client()
    return Order.objects.create(
        client=client_obj,
        title="Корпуса для датчиков",
        price=Decimal(price),
        status=OrderStatus.first(),
        **extra,
    )


@pytest.mark.django_db
def test_default_statuses_are_seeded():
    assert OrderStatus.objects.count() >= 2
    assert OrderStatus.first() is not None


@pytest.mark.django_db
def test_manager_creates_client_with_contact(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")

    resp = client.post(
        reverse("client_create"),
        {"kind": "ul", "name": "ООО Вектор", "inn": "7701234567", "tags": "опт, постоянный"},
    )
    assert resp.status_code == 302
    created = Client.objects.get(name="ООО Вектор")
    assert created.tag_list == ["опт", "постоянный"]
    assert AuditLog.objects.filter(action=AuditLog.Action.CLIENT_CREATE).exists()

    resp = client.post(
        reverse("contact_create", args=[created.pk]),
        {"name": "Мария", "phone": "+7 900 000-00-00", "is_main": "on"},
    )
    assert resp.status_code == 302
    assert created.main_contact.name == "Мария"

    page = client.get(reverse("client_detail", args=[created.pk]))
    assert page.status_code == 200
    body = page.content.decode()
    assert "ООО Вектор" in body and "Мария" in body


@pytest.mark.django_db
def test_only_one_main_contact(client):
    owner = make_client()
    ContactPerson.objects.create(client=owner, name="Первый", is_main=True)
    ContactPerson.objects.create(client=owner, name="Второй", is_main=True)
    assert list(owner.contacts.filter(is_main=True).values_list("name", flat=True)) == ["Второй"]


@pytest.mark.django_db
def test_clients_list_search_and_archive(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    vector = make_client("ООО Вектор")
    make_client("ИП Сидоров")

    body = client.get(reverse("clients"), {"q": "Вектор"}).content.decode()
    assert "ООО Вектор" in body and "ИП Сидоров" not in body

    assert client.post(reverse("client_archive", args=[vector.pk])).status_code == 302
    vector.refresh_from_db()
    assert vector.is_archived
    assert reverse("client_detail", args=[vector.pk]) not in client.get(reverse("clients")).content.decode()


@pytest.mark.django_db
def test_manager_creates_order_with_default_status_and_number(client):
    manager = make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    owner = make_client()

    resp = client.post(
        reverse("order_create"),
        {"client": owner.pk, "title": "Корпуса", "price": "15000", "tax_percent": "6"},
    )
    assert resp.status_code == 302
    order = Order.objects.get()
    assert order.number == f"З-{order.pk:04d}"
    assert order.status == OrderStatus.first()
    assert order.created_by == manager
    assert order.events.filter(kind=OrderEvent.Kind.SYSTEM).exists()
    assert resp["Location"] == reverse("order_detail", args=[order.pk])


@pytest.mark.django_db
def test_order_rejects_contact_of_another_client(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    owner, other = make_client("А"), make_client("Б")
    stranger = ContactPerson.objects.create(client=other, name="Чужой")

    resp = client.post(
        reverse("order_create"),
        {"client": owner.pk, "contact": stranger.pk, "title": "X", "price": "1", "tax_percent": "0"},
    )
    assert resp.status_code == 200
    assert Order.objects.count() == 0


@pytest.mark.django_db
def test_status_change_writes_event_and_audit(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order()
    target = OrderStatus.objects.exclude(pk=order.status_id).first()

    resp = client.post(reverse("order_status", args=[order.pk]), {"status": target.pk})
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == target
    assert order.events.filter(kind=OrderEvent.Kind.STATUS).count() == 1
    assert AuditLog.objects.filter(action=AuditLog.Action.ORDER_STATUS).exists()


@pytest.mark.django_db
def test_payments_and_rest(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order(price="10000")

    resp = client.post(
        reverse("payment_add", args=[order.pk]),
        {"date": "2026-11-02", "amount": "4000", "method": "card"},
    )
    assert resp.status_code == 302
    assert order.paid == Decimal("4000")
    assert order.rest == Decimal("6000")

    payment = order.payments.get()
    client.post(reverse("payment_delete", args=[payment.pk]))
    assert order.rest == Decimal("10000")


@pytest.mark.django_db
def test_cost_and_margin(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order(price="10000", tax_percent=Decimal("6"))

    resp = client.post(reverse("cost_add", args=[order.pk]), {"kind": "plastic", "amount": "2000"})
    assert resp.status_code == 302
    assert order.cost_total == Decimal("2600")  # 2000 + налог 6% от цены (600)
    assert order.margin == Decimal("7400")


@pytest.mark.django_db
def test_item_printed_is_clamped_and_available_to_operator(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    order = make_order()
    item = OrderItem.objects.create(order=order, name="Корпус", quantity=10, unit_price=Decimal("100"))

    client.post(reverse("item_printed", args=[item.pk]), {"printed": "99"})
    item.refresh_from_db()
    assert item.printed == 10

    client.post(reverse("item_printed", args=[item.pk]), {"printed": "-5"})
    item.refresh_from_db()
    assert item.printed == 0


@pytest.mark.django_db
def test_operator_sees_orders_without_client_and_money(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    order = make_order(price="12345")
    OrderItem.objects.create(order=order, name="Корпус", quantity=2, unit_price=Decimal("777"))

    listing = client.get(reverse("orders")).content.decode()
    assert order.number in listing
    assert "ООО Вектор" not in listing
    assert "12345" not in listing
    assert "Добавить заказ" not in listing

    detail = client.get(reverse("order_detail", args=[order.pk]), {"tab": "money"})
    assert detail.status_code == 200
    body = detail.content.decode()
    assert "ООО Вектор" not in body
    assert "12345" not in body and "777" not in body
    assert "Платежи" not in body and "Себестоимость" not in body

    assert client.get(reverse("clients")).status_code == 403
    assert client.get(reverse("client_detail", args=[order.client_id])).status_code == 403
    assert client.get(reverse("order_create")).status_code == 403
    assert client.get(reverse("payment_add", args=[order.pk])).status_code == 403
    assert (
        client.post(reverse("cost_add", args=[order.pk]), {"kind": "plastic", "amount": "1"}).status_code
        == 403
    )


@pytest.mark.django_db
def test_operator_works_with_status_checklist_and_comments(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    order = make_order()
    target = OrderStatus.objects.exclude(pk=order.status_id).first()

    assert client.post(reverse("order_status", args=[order.pk]), {"status": target.pk}).status_code == 302
    client.post(reverse("checklist_add", args=[order.pk]), {"title": "Подготовить модель"})
    step = OrderChecklistItem.objects.get()
    client.post(reverse("checklist_toggle", args=[step.pk]))
    step.refresh_from_db()
    assert step.done
    assert order.checklist_progress == (1, 1)

    client.post(reverse("order_comment", args=[order.pk]), {"text": "Пластик закончился"})
    assert order.events.filter(kind=OrderEvent.Kind.COMMENT, text="Пластик закончился").exists()


@pytest.mark.django_db
def test_user_without_role_is_forbidden(client):
    make_user("igor")
    login(client, "igor")
    order = make_order()
    for name, args in [
        ("clients", []),
        ("client_create", []),
        ("orders", []),
        ("order_create", []),
        ("order_detail", [order.pk]),
    ]:
        assert client.get(reverse(name, args=args)).status_code == 403, name


@pytest.mark.django_db
def test_order_files_get_versions_and_are_downloadable(client, settings):
    media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    settings.MEDIA_ROOT = media.name
    make_user("anna", ROLE_ADMIN)
    login(client, "anna")
    order = make_order()

    for content in (b"v1", b"v2"):
        resp = client.post(
            reverse("file_add", args=[order.pk]),
            {"title": "Корпус.stl", "comment": "", "file": SimpleUploadedFile("body.stl", content)},
        )
        assert resp.status_code == 302

    assert list(order.files.order_by("version").values_list("version", flat=True)) == [1, 2]
    latest = OrderFile.objects.get(version=2)
    download = client.get(reverse("file_download", args=[latest.pk]))
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == b"v2"

    body = client.get(reverse("order_detail", args=[order.pk]), {"tab": "files"}).content.decode()
    assert "v2" in body and "актуальная" in body


@pytest.mark.django_db
def test_menu_has_crm_links_for_manager_only(client):
    make_user("anna", ROLE_MANAGER)
    make_user("sergey", ROLE_OPERATOR)

    login(client, "anna")
    body = client.get(reverse("dashboard")).content.decode()
    for label in ("Клиенты", "Добавить клиента", "Заказы", "Добавить заказ"):
        assert label in body, label

    client.post(reverse("logout"))
    login(client, "sergey")
    body = client.get(reverse("dashboard")).content.decode()
    assert ">Заказы<" in body
    assert "Добавить клиента" not in body and "Добавить заказ" not in body
