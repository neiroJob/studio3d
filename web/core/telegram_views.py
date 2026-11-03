import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.roles import right_required
from printers.state import read_state, view_state

from . import audit
from .telegram_access import allowed_printers, can_receive, issue_pairing
from .telegram_forms import TelegramRecipientForm, TelegramSettingsForm
from .telegram_models import (
    TelegramDelivery,
    TelegramEvent,
    TelegramPairing,
    TelegramRecipient,
    TelegramRuntime,
    TelegramSettings,
)


@right_required("integrations_manage")
def manage(request):
    config = TelegramSettings.load()
    form = TelegramSettingsForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        config = form.save()
        if not config.enabled:
            TelegramDelivery.objects.filter(sent_at=None, skipped=False).update(
                skipped=True,
                last_error="Автоматические уведомления выключены",
            )
        audit.log(request, audit.Action.SETTINGS, target="Telegram", fields=list(form.changed_data))
        messages.success(request, "Настройки Telegram сохранены.")
        return redirect("telegram_settings")
    runtime = TelegramRuntime.load()
    return render(
        request,
        "core/telegram_settings.html",
        {
            "form": form,
            "runtime": runtime,
            "worker_alive": bool(
                runtime.heartbeat and timezone.now() - runtime.heartbeat < timedelta(seconds=90)
            ),
            "token_configured": bool(settings.TELEGRAM_BOT_TOKEN),
            "recipients": TelegramRecipient.objects.select_related("user").prefetch_related("printers"),
            "deliveries": TelegramDelivery.objects.select_related("recipient", "event__printer").order_by(
                "-pk"
            )[:30],
        },
    )


@right_required("integrations_manage")
def recipient_edit(request, pk=None):
    recipient = get_object_or_404(TelegramRecipient, pk=pk) if pk else None
    form = TelegramRecipientForm(request.POST or None, instance=recipient)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if "kind" in form.changed_data or "user" in form.changed_data:
            obj.chat_id = None
        obj.save()
        form.save_m2m()
        if "kind" in form.changed_data or "user" in form.changed_data:
            TelegramPairing.objects.filter(recipient=obj).delete()
        audit.log(request, audit.Action.SETTINGS, target="Telegram: " + obj.name)
        messages.success(request, "Получатель сохранён. Для нового чата создайте одноразовый код привязки.")
        return redirect("telegram_settings")
    return render(request, "core/telegram_recipient.html", {"form": form, "recipient": recipient})


@right_required("integrations_manage")
@require_POST
@never_cache
def pairing(request, pk):
    recipient = get_object_or_404(TelegramRecipient.objects.select_related("user"), pk=pk)
    if not can_receive(recipient):
        messages.error(request, "Сначала включите получателя и проверьте права сотрудника.")
        return redirect("telegram_settings")
    token = issue_pairing(recipient)
    username = TelegramRuntime.load().bot_username
    return render(
        request,
        "core/telegram_pairing.html",
        {
            "recipient": recipient,
            "token": token,
            "username": username,
            "deep_link": f"https://t.me/{username}?start={token}"
            if username and recipient.kind == "private"
            else "",
        },
    )


@right_required("integrations_manage")
@require_POST
def unlink(request, pk):
    recipient = get_object_or_404(TelegramRecipient, pk=pk)
    recipient.chat_id = None
    recipient.save(update_fields=["chat_id"])
    TelegramPairing.objects.filter(recipient=recipient).delete()
    TelegramDelivery.objects.filter(recipient=recipient, sent_at=None).update(skipped=True)
    audit.log(request, audit.Action.SETTINGS, target="Telegram: отвязка " + recipient.name)
    messages.success(request, "Чат отвязан. Старый код больше не действует.")
    return redirect("telegram_settings")


@right_required("integrations_manage")
@require_POST
def test_recipient(request, pk):
    recipient = get_object_or_404(TelegramRecipient.objects.select_related("user"), pk=pk)
    printer = allowed_printers(recipient).first()
    if not can_receive(recipient) or not recipient.chat_id or printer is None:
        messages.error(request, "Нужны привязанный доступный чат и хотя бы один принтер.")
        return redirect("telegram_settings")
    event = TelegramEvent.objects.create(
        printer=printer,
        key=uuid.uuid4().hex,
        kind="test",
        title="Проверка уведомлений",
        payload=view_state(printer, read_state(printer.pk)),
        priority=0,
    )
    TelegramDelivery.objects.create(event=event, recipient=recipient, next_attempt_at=timezone.now())
    messages.success(request, "Тест поставлен в очередь бота. Результат появится в журнале доставки ниже.")
    return redirect("telegram_settings")
