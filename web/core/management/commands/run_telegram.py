import asyncio
import logging
import uuid

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.telegram_bot import TelegramService, database
from core.telegram_models import TelegramRuntime

log = logging.getLogger(__name__)
LOCK = "telegram:worker:lock"


async def heartbeat(owner):
    while True:
        if await database(cache.get, LOCK) != owner:
            raise RuntimeError("Telegram worker lock lost")
        await database(cache.touch, LOCK, 60)
        await database(TelegramRuntime.objects.filter(pk=1).update, heartbeat=timezone.now())
        await asyncio.sleep(5)


async def run_bot():
    owner = uuid.uuid4().hex
    if not await database(cache.add, LOCK, owner, 60):
        log.warning("Другой сервис Telegram уже запущен")
        return
    await database(TelegramRuntime.load)
    beat = asyncio.create_task(heartbeat(owner))
    bot = None
    tasks = []
    try:
        while not settings.TELEGRAM_BOT_TOKEN:
            await database(
                TelegramRuntime.objects.filter(pk=1).update,
                last_error="Укажите TELEGRAM_BOT_TOKEN в .env и пересоздайте контейнер bot.",
            )
            await setup_wait(beat, 20)
        try:
            bot = Bot(settings.TELEGRAM_BOT_TOKEN, session=AiohttpSession(timeout=45))
        except Exception:
            await database(
                TelegramRuntime.objects.filter(pk=1).update,
                last_error="Некорректный формат токена. Проверьте .env и перезапустите bot.",
            )
            return
        while True:
            try:
                me = await bot.get_me()
                webhook = await bot.get_webhook_info()
                if webhook.url:
                    await database(
                        TelegramRuntime.objects.filter(pk=1).update,
                        last_error="У бота установлен webhook. Нужен отдельный бот без webhook.",
                    )
                    await setup_wait(beat, 20)
                    continue
                runtime = await database(TelegramRuntime.load)
                fields = {"bot_id": me.id, "bot_username": me.username or "", "last_error": ""}
                if runtime.bot_id != me.id:
                    fields["update_offset"] = 0
                await database(TelegramRuntime.objects.filter(pk=1).update, **fields)
                break
            except Exception as error:
                log.warning("Telegram connection failed: %s", type(error).__name__)
                await database(
                    TelegramRuntime.objects.filter(pk=1).update,
                    last_error="Не удалось подключить бота. Проверьте токен и доступ к Telegram.",
                )
                await setup_wait(beat, 10)
        commands = [
            ("status", "Состояние всех принтеров"),
            ("photo", "Свежий снимок камеры"),
            ("alerts", "Требует внимания"),
            ("history", "Последние задания"),
            ("hours", "Наработка"),
            ("light", "Только включить или выключить свет"),
            ("mute", "Выключить мои уведомления"),
            ("unmute", "Включить мои уведомления"),
            ("help", "Помощь и команды"),
        ]
        await bot.set_my_commands(
            [BotCommand(command=command, description=text) for command, text in commands]
        )
        service = TelegramService(bot)
        service.username = me.username or ""
        tasks = [beat, asyncio.create_task(service.poll()), asyncio.create_task(service.notifications())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    finally:
        for task in [beat, *tasks]:
            task.cancel()
        await asyncio.gather(beat, *tasks, return_exceptions=True)
        if bot:
            await bot.session.close()
        if await database(cache.get, LOCK) == owner:
            await database(cache.delete, LOCK)


async def setup_wait(beat, seconds):
    done, _ = await asyncio.wait({beat}, timeout=seconds)
    for task in done:
        task.result()


class Command(BaseCommand):
    help = "Telegram-бот оператора: запросы, фото, события и только управление светом"

    def handle(self, *args, **options):
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            pass
