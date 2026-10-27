import asyncio
import logging

from django.core.management.base import BaseCommand

from printers.collector import run_forever


class Command(BaseCommand):
    help = "Коллектор: постоянные соединения с принтерами."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        try:
            asyncio.run(run_forever())
        except KeyboardInterrupt:
            pass
