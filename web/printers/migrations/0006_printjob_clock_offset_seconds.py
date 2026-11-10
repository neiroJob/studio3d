from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("printers", "0005_maintenance")]
    operations = [
        migrations.AddField(
            model_name="printjob",
            name="clock_offset_seconds",
            field=models.FloatField(default=0, verbose_name="Поправка часов принтера, с"),
        )
    ]
