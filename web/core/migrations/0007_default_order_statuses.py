from django.db import migrations

# (название, цвет, конечный, побочный) — цепочка из ROADMAP.md, раздел «CRM»
STATUSES = [
    ("Новый", "--st-idle", False, False),
    ("На обсуждении", "--st-idle", False, False),
    ("Ждёт оплаты", "--st-pause", False, False),
    ("В очереди", "--st-idle", False, False),
    ("В печати", "--st-print", False, False),
    ("Постобработка", "--st-pause", False, False),
    ("Готов, ждёт выдачи", "--st-done", False, False),
    ("Выдан или отправлен", "--st-done", False, False),
    ("Закрыт", "--st-off", True, False),
    ("На паузе", "--st-pause", False, True),
    ("Отменён", "--st-off", True, True),
]


def create_statuses(apps, schema_editor):
    OrderStatus = apps.get_model("core", "OrderStatus")
    for index, (name, color, is_final, is_side) in enumerate(STATUSES, start=1):
        OrderStatus.objects.get_or_create(
            name=name, defaults={"order": index, "color": color, "is_final": is_final, "is_side": is_side}
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0006_crm")]

    operations = [migrations.RunPython(create_statuses, migrations.RunPython.noop)]
