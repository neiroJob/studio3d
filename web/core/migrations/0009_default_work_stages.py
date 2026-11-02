from django.db import migrations

# Общий список этапов работ (docs/mockups/crm_path.md): порядок фиксирован, менять может только Админ
STAGES = [
    "Проектирование / создание 3D-модели",
    "Согласование модели / чертежа",
    "В печати",
    "Распечатано",
    "Постобработка",
    "Упаковка",
    "Отправка",
]


def create_stages(apps, schema_editor):
    WorkStage = apps.get_model("core", "WorkStage")
    for index, name in enumerate(STAGES, start=1):
        WorkStage.objects.get_or_create(name=name, defaults={"order": index})


class Migration(migrations.Migration):
    dependencies = [("core", "0008_stages_and_item_stock")]

    operations = [migrations.RunPython(create_stages, migrations.RunPython.noop)]
