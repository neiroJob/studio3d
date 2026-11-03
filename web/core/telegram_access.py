import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import has_right
from printers.models import Printer

from .telegram_models import TelegramPairing, TelegramRecipient


def can_receive(recipient):
    return recipient.enabled and (
        recipient.kind == "group"
        or (recipient.user is not None and has_right(recipient.user, "view_dashboard"))
    )


def allowed_printers(recipient):
    selected = recipient.printers.all()
    return selected if selected.exists() else Printer.objects.all()


def issue_pairing(recipient):
    token = secrets.token_urlsafe(24)
    TelegramPairing.objects.update_or_create(
        recipient=recipient,
        defaults={
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": timezone.now() + timedelta(minutes=10),
        },
    )
    return token


@transaction.atomic
def pair_chat(token, chat_id, chat_type, sender_id):
    pairing = (
        TelegramPairing.objects.select_for_update()
        .select_related("recipient__user")
        .filter(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if pairing is None:
        raise ValidationError("Код недействителен или истёк. Получите новый код у администратора.")
    recipient = pairing.recipient
    if not can_receive(recipient):
        raise PermissionDenied
    if recipient.kind == "private":
        if chat_type != "private" or chat_id != sender_id:
            raise ValidationError("Этот код предназначен для личного чата сотрудника.")
    elif chat_type not in ("group", "supergroup"):
        raise ValidationError("Этот код предназначен для группы операторов.")
    if TelegramRecipient.objects.exclude(pk=recipient.pk).filter(chat_id=chat_id).exists():
        raise ValidationError("Этот чат уже привязан. Сначала измените его настройки в проекте.")
    recipient.chat_id = chat_id
    recipient.save(update_fields=["chat_id"])
    pairing.delete()
    return recipient.name


def chat_access(chat_id, sender_id):
    recipient = TelegramRecipient.objects.select_related("user").filter(chat_id=chat_id, enabled=True).first()
    if recipient is None or not can_receive(recipient):
        raise PermissionDenied
    if recipient.kind == "private" and chat_id != sender_id:
        raise PermissionDenied
    return recipient


def light_access(chat_id, sender_id, printer_id):
    chat = chat_access(chat_id, sender_id)
    actor = (
        TelegramRecipient.objects.select_related("user")
        .filter(
            chat_id=sender_id,
            kind="private",
            enabled=True,
        )
        .first()
    )
    if actor is None or not can_receive(actor) or not has_right(actor.user, "print_control"):
        raise PermissionDenied
    if not allowed_printers(chat).filter(pk=printer_id).exists():
        raise PermissionDenied
    if not allowed_printers(actor).filter(pk=printer_id).exists():
        raise PermissionDenied
    return actor.user
