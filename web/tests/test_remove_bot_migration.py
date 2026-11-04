"""Upgrading a deployed bot database preserves mobile sessions and print history."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_remove_bot_tables_keeps_mobile_and_printer_data():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    before = [("core", "0012_mobile_sessions")]
    executor.migrate(before)
    apps = executor.loader.project_state(before).apps
    now = timezone.now()
    try:
        user = apps.get_model("auth", "User").objects.create(username="migration-operator")
        printer = apps.get_model("printers", "Printer").objects.create(
            name="Migration printer",
            profile_id="qidi_q1_pro",
            host="127.0.0.1",
            port=7125,
            print_seconds_base=3600,
        )
        apps.get_model("printers", "PrintJob").objects.create(
            printer=printer,
            external_id="migration-job",
            filename="sample.gcode",
            status="completed",
            started_at=now,
            ended_at=now,
            print_duration=120,
        )
        apps.get_model("core", "MobileSession").objects.create(
            user=user,
            token_hash="a" * 64,
            auth_hash="b" * 64,
            device="Test Android",
            expires_at=now,
            last_used_at=now,
        )
        apps.get_model("core", "TelegramSettings").objects.create(
            pk=1,
            enabled=True,
            token_encrypted="synthetic-encrypted-token",
            bot_started=True,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        final = executor.loader.project_state(latest).apps
        assert not any(name.startswith("core_telegram") for name in connection.introspection.table_names())
        assert final.get_model("core", "MobileSession").objects.get(user_id=user.pk).token_hash == "a" * 64
        assert final.get_model("printers", "Printer").objects.get(pk=printer.pk).print_seconds_base == 3600
        assert (
            final.get_model("printers", "PrintJob").objects.get(printer_id=printer.pk).print_duration == 120
        )
    finally:
        MigrationExecutor(connection).migrate(latest)
