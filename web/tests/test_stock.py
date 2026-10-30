"""Тесты каркаса склада: группы и позиции (ROADMAP.md, «Склад»)."""

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.models import AuditLog, StockGroup, StockItem

PASSWORD = "Pech-2026-studio"


def make_user(username, *roles):
    user = User.objects.create_user(username=username, password=PASSWORD, first_name=username)
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def login(client, username):
    assert client.post(reverse("login"), {"username": username, "password": PASSWORD}).status_code == 302


@pytest.mark.django_db
def test_stock_page_needs_income_or_writeoff_right(client):
    make_user("denis", ROLE_ADMIN)
    make_user("anna", ROLE_MANAGER)
    make_user("sergey", ROLE_OPERATOR)
    make_user("igor")  # без ролей — нет прав

    login(client, "igor")
    assert client.get(reverse("stock")).status_code == 403

    client.post(reverse("logout"))
    login(client, "sergey")  # у оператора stock_writeoff — страницу видит
    resp = client.get(reverse("stock"))
    assert resp.status_code == 200
    assert "Добавить группу" not in resp.content.decode()  # но не stock_income

    client.post(reverse("logout"))
    login(client, "anna")  # у менеджера stock_income — видит и управляет каталогом
    resp = client.get(reverse("stock"))
    assert resp.status_code == 200
    assert "Добавить группу" in resp.content.decode()


@pytest.mark.django_db
def test_manager_creates_group_and_item(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")

    resp = client.post(reverse("stock_group_create"), {"name": "Пластик", "order": 0})
    assert resp.status_code == 302
    group = StockGroup.objects.get(name="Пластик")
    assert AuditLog.objects.filter(action="stock_group_create", target="Пластик").exists()

    resp = client.post(
        reverse("stock_item_create"),
        {
            "group": group.pk,
            "name": "PLA белый",
            "unit": "г",
            "quantity": "1000",
            "min_quantity": "200",
            "note": "",
        },
    )
    assert resp.status_code == 302
    item = StockItem.objects.get(name="PLA белый")
    assert item.group == group
    assert str(item.quantity) == "1000.00"
    assert not item.is_low
    assert AuditLog.objects.filter(action="stock_item_create", target="PLA белый").exists()


@pytest.mark.django_db
def test_low_stock_marked_and_archive_toggle(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    group = StockGroup.objects.create(name="Расходники")
    item = StockItem.objects.create(group=group, name="Сопло 0.4", unit="шт", quantity=1, min_quantity=3)

    resp = client.get(reverse("stock"))
    assert "мало" in resp.content.decode()

    resp = client.post(reverse("stock_item_archive", args=[item.pk]))
    assert resp.status_code == 302
    item.refresh_from_db()
    assert item.is_archived
    assert AuditLog.objects.filter(action="stock_item_archive", target="Сопло 0.4").exists()


@pytest.mark.django_db
def test_operator_cannot_manage_catalog(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    group = StockGroup.objects.create(name="Запчасти")

    assert client.post(reverse("stock_group_create"), {"name": "Другая", "order": 0}).status_code == 403
    assert client.post(reverse("stock_group_delete", args=[group.pk])).status_code == 403
    assert client.get(reverse("stock_item_create")).status_code == 403


@pytest.mark.django_db
def test_group_with_items_cannot_be_deleted(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    group = StockGroup.objects.create(name="Пластик")
    StockItem.objects.create(group=group, name="PETG чёрный", unit="г", quantity=500)

    resp = client.post(reverse("stock_group_delete", args=[group.pk]))
    assert resp.status_code == 302
    assert StockGroup.objects.filter(pk=group.pk).exists()  # есть позиции — не удалена

    StockItem.objects.filter(group=group).delete()
    resp = client.post(reverse("stock_group_delete", args=[group.pk]))
    assert resp.status_code == 302
    assert not StockGroup.objects.filter(pk=group.pk).exists()
