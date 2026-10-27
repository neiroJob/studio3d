import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = "Ждать, пока база данных начнёт принимать соединения."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=60)

    def handle(self, *args, timeout, **options):
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection.ensure_connection()
                self.stdout.write("База данных доступна.")
                return
            except OperationalError as exc:
                if time.monotonic() > deadline:
                    raise CommandError(f"База данных недоступна: {exc}") from exc
                time.sleep(1)
