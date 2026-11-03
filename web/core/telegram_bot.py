"""Telegram: запросы оператора, только свет из управления и доставка очереди."""

import asyncio
import logging
from datetime import timedelta

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.utils import timezone

from printers.drivers import base, client_for
from printers.models import Printer
from printers.state import read_state, view_state, write_state

from . import audit
from .models import StudioSettings
from .telegram_access import allowed_printers, can_receive, chat_access, light_access, pair_chat
from .telegram_events import refresh_notifications
from .telegram_format import clock, duration, status_text
from .telegram_models import TelegramDelivery, TelegramRecipient, TelegramRuntime, TelegramSettings
from .telegram_photos import PhotoProvider, PhotoUnavailable

log = logging.getLogger(__name__)
HELP = (
    "Студия 3D — наблюдение за печатью\n"
    "/status — все принтеры; /status ID — подробно\n"
    "/photo ID — состояние и свежий снимок\n"
    "/alerts — что требует внимания\n"
    "/history ID — последние задания\n"
    "/hours ID — наработка\n"
    "/light ID on или off — только свет\n"
    "/mute и /unmute — мои автоматические сообщения\n"
    "Вместо ID можно выбрать принтер кнопкой.\n"
    "Запуск, пауза, отмена, нагрев и снятие изделия через бота недоступны."
)


async def database(function, *args, **kwargs):
    def run():
        close_old_connections()
        try:
            return function(*args, **kwargs)
        finally:
            close_old_connections()

    return await sync_to_async(run, thread_sensitive=True)()


def snapshots(chat_id, sender_id, printer_id=None):
    recipient = chat_access(chat_id, sender_id)
    printers = allowed_printers(recipient)
    if printer_id is not None:
        printers = printers.filter(pk=printer_id)
        if not printers.exists():
            raise PermissionDenied
    zone = StudioSettings.load().timezone
    items = []
    for printer in printers:
        can_light = False
        if printer.can("light"):
            try:
                light_access(chat_id, sender_id, printer.pk)
                can_light = True
            except PermissionDenied:
                pass
        items.append(
            {
                "printer": printer,
                "state": view_state(printer, read_state(printer.pk)),
                "can_light": can_light,
                "zone": zone,
            }
        )
    return items


def control_light(chat_id, sender_id, printer_id, mode):
    if mode not in ("on", "off"):
        raise ValidationError("Режим света: on или off.")
    actor = light_access(chat_id, sender_id, printer_id)
    printer = Printer.objects.get(pk=printer_id)
    if not printer.can("light"):
        raise ValidationError("Для этого принтера управление светом не настроено.")
    client_for(printer).set_light(mode == "on")
    printer.light_hint = mode == "on"
    printer.save(update_fields=["light_hint"])
    state = read_state(printer.pk)
    if state:
        state["light"] = mode == "on"
        write_state(printer.pk, state)
    audit.log(
        None, audit.Action.PRINTER_LIGHT, user=actor, target=printer.name, on=mode == "on", via="telegram"
    )
    return f"{printer.name}: свет {'включён' if mode == 'on' else 'выключен'}."


def history_text(chat_id, sender_id, printer_id, hours=False):
    item = snapshots(chat_id, sender_id, printer_id)[0]
    printer = item["printer"]
    if hours:
        return f"{printer.name}\nПечать: {printer.print_hours:.1f} ч\nВключение: {printer.on_hours:.1f} ч"
    rows = list(printer.jobs.order_by("-ended_at")[:5])
    lines = [f"{printer.name}: последние задания"]
    for job in rows:
        name = job.filename.replace("\\", "/").rsplit("/", 1)[-1]
        lines.append(
            f"{clock(job.ended_at, item['zone'])} · {job.get_status_display()} · "
            f"{duration(job.print_duration)}\n{name}"
        )
    return "\n".join(lines) if rows else f"{printer.name}: заданий в истории пока нет."


def set_subscription(chat_id, sender_id, enabled):
    recipient = chat_access(chat_id, sender_id)
    if recipient.kind != "private":
        raise ValidationError("Групповые уведомления настраивает администратор в проекте.")
    recipient.subscribed = enabled
    recipient.save(update_fields=["subscribed"])
    return (
        "Автоматические сообщения включены."
        if enabled
        else "Автоматические сообщения выключены; запросы доступны."
    )


def keyboard(items, action="status"):
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item['printer'].pk} · {item['printer'].name}"[:60],
                callback_data=f"{action}:{item['printer'].pk}",
            )
        ]
        for item in items[:20]
    ]
    rows += [
        [
            InlineKeyboardButton(text="Все принтеры", callback_data="summary"),
            InlineKeyboardButton(text="Требует внимания", callback_data="alerts"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def printer_keyboard(item):
    pk = item["printer"].pk
    rows = [
        [
            InlineKeyboardButton(text="Обновить", callback_data=f"status:{pk}"),
            InlineKeyboardButton(text="Фото", callback_data=f"photo:{pk}"),
        ],
        [
            InlineKeyboardButton(text="История", callback_data=f"history:{pk}"),
            InlineKeyboardButton(text="Наработка", callback_data=f"hours:{pk}"),
        ],
    ]
    if item["can_light"]:
        rows.append(
            [
                InlineKeyboardButton(text="Включить свет", callback_data=f"light:{pk}:on"),
                InlineKeyboardButton(text="Выключить свет", callback_data=f"light:{pk}:off"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_deliveries():
    config = TelegramSettings.load()
    if not config.bot_started:
        return []
    query = TelegramDelivery.objects.filter(sent_at=None, skipped=False, next_attempt_at__lte=timezone.now())
    if not config.enabled:
        query = query.filter(event__kind="test")
    return list(
        query.select_related("event__printer", "recipient__user").order_by(
            "event__created_at",
            "event__priority",
            "pk",
        )[:8]
    )


def delivery_access(delivery):
    recipient = TelegramRecipient.objects.select_related("user").get(pk=delivery.recipient_id)
    event = delivery.event
    config = TelegramSettings.load()
    if not config.bot_started:
        return None
    now = timezone.now()
    valid = can_receive(recipient) and bool(recipient.chat_id)
    valid = valid and allowed_printers(recipient).filter(pk=event.printer_id).exists()
    if event.kind != "test":
        valid = valid and config.enabled and recipient.subscribed
        valid = valid and config.destination in ("both", recipient.kind)
    expiry = timedelta(minutes=15) if event.kind in ("periodic", "reminder") else timedelta(hours=24)
    valid = valid and now - event.created_at < expiry
    if event.kind == "reminder":
        valid = valid and Printer.objects.filter(pk=event.printer_id, awaiting_removal=True).exists()
    if event.kind == "periodic":
        valid = valid and (read_state(event.printer_id) or {}).get("status") == base.PRINTING
    if not valid:
        TelegramDelivery.objects.filter(pk=delivery.pk).update(
            skipped=True, last_error="Получатель или событие больше не актуальны"
        )
        return None
    gap = 3.1 if recipient.kind == "group" else 1.1
    if recipient.last_sent_at and (now - recipient.last_sent_at).total_seconds() < gap:
        TelegramDelivery.objects.filter(pk=delivery.pk).update(
            next_attempt_at=recipient.last_sent_at + timedelta(seconds=gap),
        )
        return None
    return recipient.chat_id, StudioSettings.load().timezone


def delivery_done(delivery, error="", retry_after=None, forbidden=False):
    now = timezone.now()
    attempts = delivery.attempts + 1
    fields = {"attempts": attempts, "last_error": error}
    if not error or error.startswith("Фото:"):
        fields["sent_at"] = now
        TelegramRecipient.objects.filter(pk=delivery.recipient_id).update(last_sent_at=now, last_error=error)
        TelegramRuntime.objects.filter(pk=1).update(last_success=now)
    else:
        fields["next_attempt_at"] = now + timedelta(
            seconds=retry_after or min(300, 5 * 2 ** min(attempts, 6))
        )
        TelegramRecipient.objects.filter(pk=delivery.recipient_id).update(last_error=error)
        if forbidden:
            fields["skipped"] = True
            TelegramRecipient.objects.filter(pk=delivery.recipient_id).update(enabled=False)
    TelegramDelivery.objects.filter(pk=delivery.pk, sent_at=None).update(**fields)


class TelegramService:
    def __init__(self, bot):
        self.bot = bot
        self.photos = PhotoProvider()
        self.recipient_locks = {}
        self.username = ""

    async def text(self, chat_id, text, markup=None):
        # Telegram ограничивает текст 4096 символами, подпись фото — 1024.
        for start in range(0, len(text) or 1, 3500):
            await self.bot.send_message(
                chat_id, text[start : start + 3500] or "Нет данных", reply_markup=markup
            )

    async def printer_status(self, chat_id, sender_id, printer_id, photo=False):
        item = (await database(snapshots, chat_id, sender_id, printer_id))[0]
        text = status_text(item["printer"], item["state"], item["zone"])
        markup = printer_keyboard(item)
        if not photo:
            await self.text(chat_id, text, markup)
            return
        try:
            frame = await self.photos.get(item["printer"])
        except PhotoUnavailable as error:
            await database(snapshots, chat_id, sender_id, printer_id)
            await self.text(chat_id, text + f"\nФото недоступно: {error}", markup)
            return
        # За время получения WebRTC-кадра права могли быть отозваны.
        await database(snapshots, chat_id, sender_id, printer_id)
        caption = text[:880] + f"\nСнимок: {clock(frame.captured_at, item['zone'])}"
        await self.bot.send_photo(
            chat_id,
            BufferedInputFile(frame.data, filename="printer.jpg"),
            caption=caption,
            reply_markup=markup,
        )

    async def summary(self, chat_id, sender_id, alerts=False, action="status"):
        items = await database(snapshots, chat_id, sender_id)
        if alerts:
            items = [
                item
                for item in items
                if item["printer"].enabled
                and (
                    item["state"]["status"] in (base.ERROR, base.OFFLINE, base.FINISHED, base.PAUSED)
                    or item["state"].get("faults")
                    or item["state"].get("heating_idle")
                )
            ]
        lines = ["Требует внимания" if alerts else "Принтеры студии"]
        for item in items:
            printer, state = item["printer"], item["state"]
            job = state.get("job") or {}
            line = f"{printer.pk} · {printer.name}: {state['label']}"
            if job and state["status"] in (base.PRINTING, base.PAUSED):
                line += f" {job.get('percent', 0)} % · осталось {duration(job.get('remaining'))}"
            if not printer.enabled:
                line += " (опрос отключён)"
            lines.append(line)
        if not items:
            lines.append("Нет принтеров, требующих внимания." if alerts else "Принтеров пока нет.")
        if len(items) > 20:
            lines.append("Кнопки для первых 20; остальные доступны командой /status ID.")
        await self.text(chat_id, "\n".join(lines), keyboard(items, action))

    async def command(self, chat_id, sender_id, command, args):
        await database(chat_access, chat_id, sender_id)
        if command in ("help", "start"):
            await self.text(chat_id, HELP)
            await self.summary(chat_id, sender_id)
        elif command in ("status", "printers"):
            if args:
                await self.printer_status(chat_id, sender_id, int(args[0]))
            else:
                await self.summary(chat_id, sender_id)
        elif command in ("photo", "history", "hours", "light"):
            if not args:
                await self.summary(chat_id, sender_id, action=command if command != "light" else "status")
                return
            pk = int(args[0])
            if command == "photo":
                await self.printer_status(chat_id, sender_id, pk, photo=True)
            elif command in ("history", "hours"):
                await self.text(
                    chat_id, await database(history_text, chat_id, sender_id, pk, command == "hours")
                )
            else:
                if len(args) != 2:
                    raise ValidationError("Используйте /light ID on или /light ID off.")
                await self.text(chat_id, await database(control_light, chat_id, sender_id, pk, args[1]))
        elif command == "alerts":
            await self.summary(chat_id, sender_id, alerts=True)
        elif command in ("mute", "unmute"):
            await self.text(
                chat_id, await database(set_subscription, chat_id, sender_id, command == "unmute")
            )
        else:
            await self.text(chat_id, "Такая команда недоступна.\n" + HELP)

    async def handle(self, update):
        message = update.message
        callback = update.callback_query
        if callback:
            if callback.message is None or callback.from_user is None:
                return
            chat_id, sender_id = callback.message.chat.id, callback.from_user.id
            try:
                await self.bot.answer_callback_query(callback.id)
            except Exception:
                pass
            data = (callback.data or "").split(":")
            command, args = data[0], data[1:]
            if command == "summary":
                command = "status"
        elif message and message.from_user and not message.from_user.is_bot and message.text:
            chat_id, sender_id = message.chat.id, message.from_user.id
            parts = message.text.strip().split()
            if not parts:
                return
            head, _, mention = parts[0].partition("@")
            if mention and mention.lower() != self.username.lower():
                return
            command, args = head.lstrip("/").lower(), parts[1:]
            if command in ("start", "pair") and len(args) == 1:
                try:
                    name = await database(pair_chat, args[0], chat_id, message.chat.type, sender_id)
                    await self.text(chat_id, f"Чат привязан: {name}.\n" + HELP)
                except (ValidationError, PermissionDenied):
                    await self.text(
                        chat_id,
                        "Код не подходит, истёк или доступ запрещён. Получите новый у администратора.",
                    )
                return
        else:
            return
        try:
            await self.command(chat_id, sender_id, command, args)
        except PermissionDenied:
            await self.text(
                chat_id, "Нет доступа. Привяжите чат одноразовым кодом администратора и проверьте права."
            )
        except (ValueError, IndexError):
            await self.text(chat_id, "Укажите ID принтера из /status. Например: /photo 1.")
        except ValidationError as error:
            await self.text(chat_id, " ".join(error.messages))
        except Exception as error:
            log.warning("Telegram command failed: %s", type(error).__name__)
            await self.text(chat_id, "Не удалось выполнить запрос. Проверьте связь и повторите.")

    async def deliver(self, delivery):
        lock = self.recipient_locks.setdefault(delivery.recipient_id, asyncio.Lock())
        async with lock:
            access = await database(delivery_access, delivery)
            if access is None:
                return
            chat_id, zone = access
            event = delivery.event
            text = status_text(event.printer, event.payload, zone, event.title, event.created_at)
            note = ""
            try:
                try:
                    photo = await self.photos.get(event.printer)
                except PhotoUnavailable as error:
                    photo = None
                    note = f"Фото: {error}"
                access = await database(delivery_access, delivery)
                if access is None:
                    return
                chat_id, zone = access
                if photo:
                    caption = text[:880] + f"\nСнимок: {clock(photo.captured_at, zone)}"
                    await self.bot.send_photo(
                        chat_id, BufferedInputFile(photo.data, filename="printer.jpg"), caption=caption
                    )
                else:
                    await self.text(chat_id, text + "\n" + note)
                await database(delivery_done, delivery, note)
            except TelegramRetryAfter as error:
                await database(
                    delivery_done, delivery, "Telegram ограничил частоту отправки", error.retry_after
                )
            except TelegramForbiddenError:
                await database(delivery_done, delivery, "Бот заблокирован или удалён из чата", forbidden=True)
            except Exception as error:
                log.warning("Telegram delivery failed: %s", type(error).__name__)
                await database(delivery_done, delivery, "Ошибка связи с Telegram; будет повтор")

    async def poll(self):
        while True:
            runtime = await database(TelegramRuntime.load)
            try:
                updates = await self.bot.get_updates(
                    offset=runtime.update_offset,
                    timeout=20,
                    limit=20,
                    allowed_updates=["message", "callback_query"],
                )
                await database(TelegramRuntime.objects.filter(pk=1).update, last_error="")
                for update in updates:
                    await self.handle(update)
                    await database(
                        TelegramRuntime.objects.filter(pk=1).update, update_offset=update.update_id + 1
                    )
            except Exception as error:
                log.warning("Telegram polling failed: %s", type(error).__name__)
                await database(
                    TelegramRuntime.objects.filter(pk=1).update,
                    last_error="Нет связи с Telegram; повторяем подключение",
                )
                await asyncio.sleep(5)

    async def notifications(self):
        while True:
            try:
                await database(refresh_notifications)
                batch = await database(pending_deliveries)
                await asyncio.gather(*(self.deliver(delivery) for delivery in batch))
            except Exception as error:
                log.warning("Telegram notification worker failed: %s", type(error).__name__)
                await database(
                    TelegramRuntime.objects.filter(pk=1).update,
                    last_error="Не удалось обработать очередь уведомлений",
                )
            await asyncio.sleep(2)
