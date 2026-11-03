"""Настройки и очередь Telegram в существующем приложении core."""

from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models

from .crypto import decrypt


class TelegramSettings(models.Model):
    token_encrypted = models.TextField(blank=True, default="")
    bot_started = models.BooleanField(default=False)

    class Destination(models.TextChoices):
        PRIVATE = "private", "Личные чаты"
        GROUP = "group", "Группы"
        BOTH = "both", "Личные чаты и группы"

    enabled = models.BooleanField("Включить автоматические уведомления", default=False)
    destination = models.CharField(
        "Куда отправлять", max_length=10, choices=Destination.choices, default="private"
    )
    interval_minutes = models.PositiveIntegerField(
        "Фото и состояние каждые, мин",
        default=10,
        validators=[MaxValueValidator(1440)],
        help_text=(
            "Только во время печати. 0 — отключить периодические сообщения; события останутся включены."
        ),
    )
    reminder_minutes = models.PositiveIntegerField(
        "Напоминать о снятии изделия каждые, мин",
        default=30,
        validators=[MaxValueValidator(1440)],
        help_text="0 — без повторных напоминаний. Первое сообщение о завершении отправляется отдельно.",
    )
    offline_minutes = models.PositiveIntegerField(
        "Сообщать об отсутствии связи через, мин",
        default=1,
        validators=[MaxValueValidator(60)],
        help_text="Задержка уменьшает сообщения при кратких переподключениях. 0 — сразу.",
    )
    notify_start = models.BooleanField("Начало печати", default=True)
    notify_finish = models.BooleanField("Завершение, отмена и прерывание", default=True)
    notify_pause = models.BooleanField("Пауза и продолжение", default=True)
    notify_errors = models.BooleanField("Неисправности и отсутствие нити", default=True)
    notify_connection = models.BooleanField("Потеря и восстановление связи", default=True)

    def __str__(self):
        return "Настройки Telegram"

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)

    @property
    def token(self):
        return decrypt(self.token_encrypted)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class TelegramRecipient(models.Model):
    class Kind(models.TextChoices):
        PRIVATE = "private", "Личный чат сотрудника"
        GROUP = "group", "Группа операторов"

    name = models.CharField("Название / сотрудник", max_length=150)
    kind = models.CharField("Тип чата", max_length=10, choices=Kind.choices)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    chat_id = models.BigIntegerField(null=True, blank=True, unique=True)
    enabled = models.BooleanField("Разрешить доступ", default=True)
    subscribed = models.BooleanField("Получать автоматические уведомления", default=True)
    printers = models.ManyToManyField("printers.Printer", blank=True, help_text="Не выбрано — все принтеры.")
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(kind="private"),
                name="telegram_one_private_per_user",
            )
        ]

    def __str__(self):
        return self.name


class TelegramPairing(models.Model):
    recipient = models.OneToOneField(TelegramRecipient, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"Привязка получателя {self.recipient_id}"


class TelegramPrinterState(models.Model):
    printer = models.OneToOneField("printers.Printer", on_delete=models.CASCADE)
    snapshot = models.JSONField(default=dict)
    active = models.BooleanField(default=False)
    run_key = models.CharField(max_length=40, blank=True)
    active_since = models.DateTimeField(null=True)
    last_periodic = models.DateTimeField(null=True)
    last_reminder = models.DateTimeField(null=True)
    offline_since = models.DateTimeField(null=True)
    offline_notified = models.BooleanField(default=False)

    def __str__(self):
        return f"Состояние принтера {self.printer_id} для Telegram"


class TelegramEvent(models.Model):
    printer = models.ForeignKey("printers.Printer", on_delete=models.CASCADE)
    key = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=30)
    title = models.CharField(max_length=150)
    payload = models.JSONField(default=dict)
    priority = models.PositiveSmallIntegerField(default=10)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class TelegramDelivery(models.Model):
    event = models.ForeignKey(TelegramEvent, on_delete=models.CASCADE)
    recipient = models.ForeignKey(TelegramRecipient, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(null=True, blank=True)
    skipped = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_error = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "recipient"], name="telegram_delivery_once")]
        indexes = [models.Index(fields=["sent_at", "skipped", "next_attempt_at"])]

    def __str__(self):
        return f"Доставка {self.event_id} получателю {self.recipient_id}"


class TelegramRuntime(models.Model):
    bot_id = models.BigIntegerField(null=True, blank=True)
    bot_username = models.CharField(max_length=64, blank=True)
    heartbeat = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    update_offset = models.BigIntegerField(default=0)

    def __str__(self):
        return "Состояние сервиса Telegram"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]
