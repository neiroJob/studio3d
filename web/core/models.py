import zoneinfo

from django.conf import settings
from django.core.cache import cache
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

TZ_CACHE_KEY = "studio:timezone"


def timezone_choices():
    zones = sorted(z for z in zoneinfo.available_timezones() if z.startswith(("Europe/", "Asia/")))
    return [(z, z) for z in zones]


class StudioSettings(models.Model):
    """Настройки студии. Одна запись (pk=1)."""

    name = models.CharField("Название студии", max_length=200)
    timezone = models.CharField("Часовой пояс", max_length=64, default=settings.TIME_ZONE)
    setup_completed_at = models.DateTimeField("Мастер первого запуска пройден", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "настройки студии"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(TZ_CACHE_KEY)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"name": "Студия 3D"})
        return obj


class ElectricityTariff(models.Model):
    """Тариф электроэнергии. Версионируется по дате: старые расчёты не пересчитываются."""

    price_kwh = models.DecimalField(
        "Цена, ₽ за кВт·ч", max_digits=8, decimal_places=2, validators=[MinValueValidator(0)]
    )
    valid_from = models.DateField("Действует с")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-valid_from", "-created_at"]
        verbose_name = "тариф электроэнергии"

    def __str__(self):
        return f"{self.price_kwh} ₽/кВт·ч с {self.valid_from:%d.%m.%Y}"

    @classmethod
    def on_date(cls, date=None):
        date = date or timezone.localdate()
        return cls.objects.filter(valid_from__lte=date).first()


class AuditLog(models.Model):
    """Журнал действий: кто, когда, что (ТЗ, раздел 3)."""

    class Action(models.TextChoices):
        LOGIN = "login", "Вход"
        LOGIN_FAILED = "login_failed", "Неудачный вход"
        LOGIN_LOCKED = "login_locked", "Блокировка входа по IP"
        LOGOUT = "logout", "Выход"
        SETUP = "setup", "Мастер первого запуска"
        USER_CREATE = "user_create", "Пользователь создан"
        USER_UPDATE = "user_update", "Пользователь изменён"
        USER_PASSWORD = "user_password", "Пароль сброшен"
        ROLE_CREATE = "role_create", "Роль создана"
        ROLE_UPDATE = "role_update", "Роль изменена"
        ROLE_DELETE = "role_delete", "Роль удалена"
        SETTINGS = "settings", "Настройки студии изменены"
        TARIFF = "tariff", "Тариф добавлен"
        BACKUP_DOWNLOAD = "backup_download", "Бэкап скачан"
        PRINTER_ADD = "printer_add", "Принтер добавлен"
        PRINTER_UPDATE = "printer_update", "Принтер изменён"
        PRINTER_DELETE = "printer_delete", "Принтер удалён"
        PRINT_START = "print_start", "Печать запущена"
        PRINT_PAUSE = "print_pause", "Печать на паузе"
        PRINT_RESUME = "print_resume", "Печать продолжена"
        PRINT_CANCEL = "print_cancel", "Печать отменена"
        FILE_UPLOAD = "file_upload", "Файл загружен на принтер"
        PLATE_CLEARED = "plate_cleared", "Изделие снято"

    created_at = models.DateTimeField("Когда", auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    username = models.CharField("Кто", max_length=150, blank=True)
    action = models.CharField("Действие", max_length=32, choices=Action.choices, db_index=True)
    target = models.CharField("Объект", max_length=255, blank=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)
    details = models.JSONField("Подробности", default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "запись журнала"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username} {self.get_action_display()} {self.target}"
