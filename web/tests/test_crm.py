"""Тесты CRM: карточки клиента и заказа (ROADMAP.md, «CRM»)."""

import tempfile
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.gcode_meta import parse_gcode_meta
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
    StockGroup,
    StockItem,
    WorkStage,
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
    step = OrderChecklistItem.objects.create(order=order, title="В печати")
    client.post(reverse("checklist_toggle", args=[step.pk]))
    step.refresh_from_db()
    assert step.done
    assert order.checklist_progress == (1, 1)
    # оператор только отмечает этапы; список правит один Админ
    assert client.get(reverse("stages")).status_code == 403
    assert client.post(reverse("stages"), {"name": "Свой этап"}).status_code == 403
    assert not WorkStage.objects.filter(name="Свой этап").exists()

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


def make_plastic(quantity="2", unit="кг", **extra):
    group, _ = StockGroup.objects.get_or_create(name="Пластик")
    return StockItem.objects.create(
        group=group, name="PETG чёрный", unit=unit, quantity=Decimal(quantity), **extra
    )


def make_item(order, plastic=None, **extra):
    params = {"name": "Корпус", "quantity": 10, "unit_weight_g": Decimal("50"), "unit_time_min": 30}
    params.update(extra)
    return OrderItem.objects.create(order=order, plastic=plastic, unit_price=Decimal("100"), **params)


@pytest.mark.django_db
def test_default_work_stages_are_seeded_in_fixed_order():
    names = list(WorkStage.objects.values_list("name", flat=True))
    assert len(names) == 7
    assert names[2:] == ["В печати", "Распечатано", "Постобработка", "Упаковка", "Отправка"]


@pytest.mark.django_db
def test_only_admin_edits_stage_catalog(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    stage = WorkStage.objects.first()
    assert client.get(reverse("stages")).status_code == 403
    assert client.post(reverse("stage_delete", args=[stage.pk])).status_code == 403
    assert client.post(reverse("stage_move", args=[stage.pk]), {"dir": "down"}).status_code == 403
    assert WorkStage.objects.count() == 7


@pytest.mark.django_db
def test_admin_adds_moves_and_deletes_stages(client):
    make_user("root", ROLE_ADMIN)
    login(client, "root")
    assert client.get(reverse("stages")).status_code == 200

    client.post(reverse("stages"), {"name": "Контроль качества"})
    added = WorkStage.objects.get(name="Контроль качества")
    assert added.order == 8
    assert AuditLog.objects.filter(action=AuditLog.Action.STAGE_CATALOG).exists()

    client.post(reverse("stage_move", args=[added.pk]), {"dir": "up"})
    names = list(WorkStage.objects.values_list("name", flat=True))
    assert names[6] == "Контроль качества" and names[-1] == "Отправка"
    client.post(reverse("stage_move", args=[WorkStage.objects.first().pk]), {"dir": "up"})  # выше нельзя
    assert [s.order for s in WorkStage.objects.all()] == list(range(1, 9))

    client.post(reverse("stage_delete", args=[added.pk]))
    assert not WorkStage.objects.filter(name="Контроль качества").exists()


@pytest.mark.django_db
def test_new_order_copies_stages_and_old_orders_keep_theirs(client):
    make_user("root", ROLE_ADMIN)
    login(client, "root")
    owner = make_client()
    old = make_order(owner)
    OrderChecklistItem.objects.create(order=old, title="Старый этап")

    WorkStage.objects.create(name="Контроль качества", order=8)
    client.post(reverse("order_create"), {"client": owner.pk, "title": "Новый", "price": "1", "tax_percent": "0"})
    new = Order.objects.exclude(pk=old.pk).get()
    titles = list(new.checklist.values_list("title", flat=True))
    assert len(titles) == 8 and titles[-1] == "Контроль качества"
    assert list(old.checklist.values_list("title", flat=True)) == ["Старый этап"]

    # правка заказа этапы не дублирует
    client.post(
        reverse("order_edit", args=[new.pk]),
        {"client": owner.pk, "title": "Новый 2", "price": "2", "tax_percent": "0"},
    )
    assert new.checklist.count() == 8


@pytest.mark.django_db
def test_order_detail_has_no_free_stage_editing(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order()
    OrderChecklistItem.objects.create(order=order, title="Упаковка")
    body = client.get(reverse("order_detail", args=[order.pk])).content.decode()
    assert "Новый этап" not in body and "Убрать этап" not in body


@pytest.mark.django_db
def test_operator_cannot_create_or_edit_orders(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    owner = make_client()
    order = make_order(owner)
    resp = client.post(reverse("order_create"), {"client": owner.pk, "title": "Х", "price": "1"})
    assert resp.status_code == 403
    assert client.post(reverse("order_edit", args=[order.pk]), {"title": "Х"}).status_code == 403
    assert Order.objects.count() == 1


@pytest.mark.django_db
def test_item_form_takes_weight_and_time_from_gcode(client, settings):
    media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    settings.MEDIA_ROOT = media.name
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order()
    plastic = make_plastic()
    gcode = b"; total filament weight [g] : 12.5\n; estimated printing time (normal mode) = 1h 5m 30s\nG28\n"

    resp = client.post(
        reverse("item_create", args=[order.pk]),
        {
            "name": "Крышка",
            "quantity": "4",
            "plastic": plastic.pk,
            "new_file": SimpleUploadedFile("lid.gcode", gcode),
            "unit_price": "100",
        },
    )
    assert resp.status_code == 302
    item = order.items.get()
    assert item.unit_weight_g == Decimal("12.50") and item.unit_time_min == 66
    assert item.print_file.title == "lid.gcode" and item.print_file.version == 1
    assert item.need_grams == Decimal("50") and item.total_time_min == 264


@pytest.mark.django_db
def test_manual_weight_and_time_win_over_gcode(client, settings):
    media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    settings.MEDIA_ROOT = media.name
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order()
    gcode = b"; total filament weight [g] : 12.5\n; estimated printing time (normal mode) = 1h\n"
    client.post(
        reverse("item_create", args=[order.pk]),
        {
            "name": "Крышка",
            "quantity": "1",
            "new_file": SimpleUploadedFile("lid.gcode", gcode),
            "unit_weight_g": "20",
            "unit_time_min": "90",
            "unit_price": "1",
        },
    )
    item = order.items.get()
    assert item.unit_weight_g == Decimal("20") and item.unit_time_min == 90


@pytest.mark.django_db
def test_item_without_plastic_and_file_is_allowed(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    order = make_order()
    resp = client.post(reverse("item_create", args=[order.pk]), {"name": "Модель", "quantity": "1", "unit_price": "5"})
    assert resp.status_code == 302
    item = order.items.get()
    assert item.plastic is None and item.unit_weight_g == 0 and item.unit_time_min == 0


@pytest.mark.django_db
def test_stock_shortage_is_reserved_across_orders_and_warned(client):
    make_user("anna", ROLE_MANAGER)
    login(client, "anna")
    plastic = make_plastic("1")  # 1 кг = 1000 г
    first = make_item(make_order(), plastic)  # 500 г
    assert first.shortage_grams == 0
    second = make_item(make_order(), plastic, quantity=12)  # 600 г при свободных 500
    assert second.shortage_grams == Decimal("100")
    assert plastic.reserved_grams() == Decimal("1100")

    resp = client.post(
        reverse("item_edit", args=[second.pk]),
        {"name": "Корпус", "quantity": "12", "plastic": plastic.pk, "unit_weight_g": "50", "unit_time_min": "30", "unit_price": "100"},
        follow=True,
    )
    assert "не хватает" in resp.content.decode()


@pytest.mark.django_db
def test_printed_pieces_write_plastic_off_the_stock(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    plastic = make_plastic("2", min_quantity=Decimal("1.2"))
    item = make_item(make_order(), plastic)

    client.post(reverse("item_printed", args=[item.pk]), {"printed": "4"})  # 4 × 50 г = 200 г
    plastic.refresh_from_db()
    assert plastic.quantity == Decimal("1.80")
    assert plastic.reserved_grams() == Decimal("300")  # напечатанное резервом уже не считается

    client.post(reverse("item_printed", args=[item.pk]), {"printed": "4"})  # без изменений — без списания
    plastic.refresh_from_db()
    assert plastic.quantity == Decimal("1.80")

    resp = client.post(reverse("item_printed", args=[item.pk]), {"printed": "10"}, follow=True)  # ещё 300 г
    plastic.refresh_from_db()
    assert plastic.quantity == Decimal("1.50")
    assert AuditLog.objects.filter(action=AuditLog.Action.ORDER_STOCK).count() == 2

    client.post(reverse("item_printed", args=[item.pk]), {"printed": "0"})  # исправили вниз — вернули
    plastic.refresh_from_db()
    assert plastic.quantity == Decimal("2.00")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_low_stock_is_warned_after_write_off(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    plastic = make_plastic("1", min_quantity=Decimal("0.8"))
    item = make_item(make_order(), plastic)
    resp = client.post(reverse("item_printed", args=[item.pk]), {"printed": "10"}, follow=True)  # −500 г
    assert "мало" in resp.content.decode()


@pytest.mark.django_db
def test_non_weight_stock_unit_is_not_written_off(client):
    make_user("sergey", ROLE_OPERATOR)
    login(client, "sergey")
    plastic = make_plastic("5", unit="катушка")
    item = make_item(make_order(), plastic)
    assert plastic.grams_per_unit is None and item.shortage_grams == 0
    client.post(reverse("item_printed", args=[item.pk]), {"printed": "5"})
    plastic.refresh_from_db()
    assert plastic.quantity == Decimal("5")


def test_gcode_meta_parsers():
    prusa = "; filament used [g] = 10.5, 4.5\n; estimated printing time (normal mode) = 1d 2h 3m 4s\n"
    assert parse_gcode_meta(prusa, "") == (Decimal("15.00"), 26 * 60 + 3 + 0)
    cura = ";TIME:3600\n;Filament used: 1.2m\n"
    assert parse_gcode_meta(cura, "") == (None, 60)
    assert parse_gcode_meta("G28\n", "G1 X0\n") == (None, None)
    bambu = "; total filament weight [g] : 8.80\n; total estimated time: 2h 5m 10s\n"
    assert parse_gcode_meta("", bambu) == (Decimal("8.80"), 125)


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
