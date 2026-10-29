from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum

from core.crypto import decrypt, encrypt

from .profiles import get_profile


class Printer(models.Model):
    name = models.CharField("Имя", max_length=100, unique=True)
    profile_id = models.CharField("Модель (профиль)", max_length=100)
    host = models.CharField("IP-адрес", max_length=255)
    port = models.PositiveIntegerField(
        "Порт API", validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    api_key_encrypted = models.TextField(blank=True, default="")
    zone = models.CharField("Зона", max_length=100, blank=True)
    enabled = models.BooleanField("Опрашивать", default=True)

    power_w = models.PositiveIntegerField("Мощность, Вт", null=True, blank=True)
    purchase_cost = models.DecimalField(
        "Стоимость принтера, ₽", max_digits=12, decimal_places=2, null=True, blank=True
    )
    amortization_hours = models.PositiveIntegerField("Ресурс для амортизации, ч", default=5000)

    # Наработка. Часы печати = база (история принтера на момент добавления) + задания, записанные системой.
    print_seconds_base = models.PositiveBigIntegerField("Часы печати до подключения, с", default=0)
    history_since = models.FloatField("Учитывать задания после (unix time)", default=0)
    on_seconds = models.PositiveBigIntegerField("Время включения, с", default=0)
    last_seen_at = models.DateTimeField("Последний ответ", null=True, blank=True)

    awaiting_removal = models.BooleanField("Ждёт снятия изделия", default=False)
    # Для моделей, которые не сообщают состояние света в живой телеметрии (например, K1C:
    # свет только через нативный WebSocket, отдельно от Moonraker) — последняя команда,
    # чтобы кнопка показывала состояние, а не терялась при каждом обновлении состояния.
    light_hint = models.BooleanField("Свет включён (последняя команда)", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "принтер"

    def __str__(self):
        return self.name

    @property
    def profile(self):
        return get_profile(self.profile_id)

    @property
    def api_key(self):
        return decrypt(self.api_key_encrypted)

    @api_key.setter
    def api_key(self, value):
        self.api_key_encrypted = encrypt(value or "")

    @property
    def print_seconds(self):
        counted = self.jobs.aggregate(s=Sum("print_duration"))["s"] or 0
        return self.print_seconds_base + int(counted)

    @property
    def print_hours(self):
        return self.print_seconds / 3600

    @property
    def on_hours(self):
        return self.on_seconds / 3600

    def can(self, capability):
        profile = self.profile
        return bool(profile and profile.can(capability))


class PrintJob(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Успех"
        CANCELLED = "cancelled", "Отменено"
        ERROR = "error", "Ошибка"
        KLIPPY_SHUTDOWN = "klippy_shutdown", "Аварийная остановка"
        KLIPPY_DISCONNECT = "klippy_disconnect", "Потеря связи с Klipper"
        SERVER_EXIT = "server_exit", "Перезапуск Moonraker"
        INTERRUPTED = "interrupted", "Прервано"
        OTHER = "other", "Другое"

    class Quality(models.TextChoices):
        OK = "ok", "Без замечаний"
        DEFECT = "defect", "Брак"

    class DefectReason(models.TextChoices):
        FILAMENT_RUNOUT = "filament_runout", "Кончился пластик"
        FILAMENT_TANGLE = "filament_tangle", "Запутался/застрял пластик"
        ADHESION = "adhesion", "Не прилипло к столу / отвалилось"
        SHIFTED_LAYERS = "shifted_layers", "Смещение слоёв"
        CLOG = "clog", "Забилось сопло"
        POWER_LOSS = "power_loss", "Пропадание питания"
        OTHER = "other", "Другое"

    printer = models.ForeignKey(Printer, on_delete=models.CASCADE, related_name="jobs")
    external_id = models.CharField("ID задания на принтере", max_length=64)
    filename = models.CharField("Файл", max_length=500)
    status = models.CharField("Результат", max_length=32, choices=Status.choices)
    started_at = models.DateTimeField("Начало")
    ended_at = models.DateTimeField("Окончание", null=True, blank=True)
    print_duration = models.FloatField("Время печати, с", default=0)
    total_duration = models.FloatField("Общее время, с", default=0)
    filament_mm = models.FloatField("Пластик, мм", default=0)
    filament_g = models.FloatField("Пластик, г", null=True, blank=True)

    # Заполняется человеком при снятии изделия (views.plate_cleared) — не связано со status,
    # который приходит от принтера: напечатано может быть «успешно» (status=completed), но
    # изделие всё равно оказалось бракованным (отклеилось, сместились слои и т.п.).
    quality = models.CharField("Качество печати", max_length=16, choices=Quality.choices, blank=True)
    defect_reason = models.CharField("Причина брака", max_length=32, choices=DefectReason.choices, blank=True)
    defect_note = models.TextField("Комментарий к браку", blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [models.UniqueConstraint(fields=["printer", "external_id"], name="uniq_printer_job")]
        verbose_name = "задание печати"

    def __str__(self):
        return f"{self.printer} · {self.filename}"


class PrinterFault(models.Model):
    """Fault messages remain visible until a user confirms they have acted."""

    printer = models.ForeignKey(Printer, on_delete=models.CASCADE, related_name="fault_events")
    message = models.TextField("Сообщение неисправности")
    first_seen = models.DateTimeField("Впервые замечено", auto_now_add=True)
    last_seen = models.DateTimeField("Последний раз замечено", auto_now=True)
    active = models.BooleanField("Сейчас приходит от принтера", default=True)
    acknowledged_at = models.DateTimeField("Подтверждено пользователем", null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_printer_faults",
    )

    class Meta:
        ordering = ["first_seen", "pk"]
        indexes = [
            models.Index(fields=["printer", "acknowledged_at"], name="printer_fault_pending_idx"),
            models.Index(fields=["printer", "active"], name="printer_fault_active_idx"),
        ]

    def __str__(self):
        return f"{self.printer}: {self.message[:100]}"
