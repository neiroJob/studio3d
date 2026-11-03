import zoneinfo
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from . import telegram_models  # noqa: F401 -- регистрация моделей интеграции в core

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


PLASTIC_GROUP = "Пластик"  # группа склада, из которой выбирается пластик для изделий заказа
GRAMS_PER_UNIT = {
    "кг": Decimal("1000"),
    "kg": Decimal("1000"),
    "г": Decimal("1"),
    "гр": Decimal("1"),
    "g": Decimal("1"),
}


class StockGroup(models.Model):
    """Группа позиций склада: пластик, сопла, запчасти, расходники, готовые изделия (ROADMAP.md, «Склад»)."""

    name = models.CharField("Название", max_length=100, unique=True)
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "группа склада"
        verbose_name_plural = "группы склада"

    def __str__(self):
        return self.name


class StockItem(models.Model):
    """Позиция склада. Остаток пока учитывается вручную — автосписание появится отдельной задачей."""

    group = models.ForeignKey(
        StockGroup, on_delete=models.PROTECT, related_name="items", verbose_name="Группа"
    )
    name = models.CharField("Название", max_length=200)
    unit = models.CharField("Единица", max_length=20, default="шт")
    quantity = models.DecimalField("Остаток", max_digits=10, decimal_places=2, default=0)
    min_quantity = models.DecimalField(
        "Минимальный остаток", max_digits=10, decimal_places=2, null=True, blank=True
    )
    note = models.CharField("Заметка", max_length=255, blank=True)
    is_archived = models.BooleanField("В архиве", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["group__order", "group__name", "name"]
        verbose_name = "позиция склада"
        verbose_name_plural = "позиции склада"

    def __str__(self):
        return self.name

    @property
    def is_low(self):
        return self.min_quantity is not None and self.quantity < self.min_quantity

    @property
    def grams_per_unit(self):
        """Граммов в единице учёта; `None`, если единица не вес (катушки, штуки) — граммы не пересчитать."""
        return GRAMS_PER_UNIT.get(self.unit.strip().lower().rstrip("."))

    @property
    def grams(self):
        return None if self.grams_per_unit is None else self.quantity * self.grams_per_unit

    def reserved_grams(self, exclude_item=None):
        """Резерв под заказы: ещё не напечатанная часть изделий в незавершённых заказах."""
        items = self.order_items.filter(order__status__is_final=False)
        if exclude_item is not None and exclude_item.pk:
            items = items.exclude(pk=exclude_item.pk)
        return sum((i.remaining_grams for i in items), Decimal("0"))

    def available_grams(self, exclude_item=None):
        """Свободно для нового резерва: остаток минус резерв других изделий. `None` — не считается."""
        grams = self.grams
        return None if grams is None else grams - self.reserved_grams(exclude_item)


# --- CRM: клиенты и заказы (ROADMAP.md, «CRM») -------------------------------------------------


class Client(models.Model):
    """Карточка клиента. Набор реквизитов зависит от типа (юрлицо, ИП, самозанятый, физлицо)."""

    class Kind(models.TextChoices):
        COMPANY = "ul", "Юрлицо"
        SOLE = "ip", "ИП"
        SELF_EMPLOYED = "sz", "Самозанятый"
        PERSON = "fl", "Физлицо"

    kind = models.CharField("Тип", max_length=2, choices=Kind.choices, default=Kind.COMPANY)
    name = models.CharField("Название или ФИО", max_length=255)
    inn = models.CharField("ИНН", max_length=12, blank=True)
    kpp = models.CharField("КПП", max_length=9, blank=True)
    ogrn = models.CharField("ОГРН / ОГРНИП", max_length=15, blank=True)
    address = models.CharField("Юридический адрес", max_length=255, blank=True)
    bank = models.CharField("Банк", max_length=200, blank=True)
    bik = models.CharField("БИК", max_length=9, blank=True)
    account = models.CharField("Расчётный счёт", max_length=34, blank=True)
    signer = models.CharField("Подписант", max_length=200, blank=True)
    tax_regime = models.CharField("Налоговый режим", max_length=100, blank=True)
    pd_consent = models.BooleanField("Согласие на обработку персональных данных", default=False)
    tags = models.CharField("Теги", max_length=255, blank=True, help_text="Через запятую")
    next_contact = models.DateField("Следующий контакт", null=True, blank=True)
    is_archived = models.BooleanField("В архиве", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "клиент"
        verbose_name_plural = "клиенты"

    def __str__(self):
        return self.name

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def main_contact(self):
        contacts = list(self.contacts.all())
        return next((c for c in contacts if c.is_main), contacts[0] if contacts else None)

    @property
    def requisites(self):
        """Заполненные реквизиты в порядке показа: (подпись, значение)."""
        rows = [
            ("ИНН", self.inn),
            ("КПП", self.kpp),
            ("ОГРНИП" if self.kind == self.Kind.SOLE else "ОГРН", self.ogrn),
            ("Адрес", self.address),
            ("Банк", self.bank),
            ("БИК", self.bik),
            ("Расчётный счёт", self.account),
            ("Подписант", self.signer),
            ("Налоговый режим", self.tax_regime),
        ]
        return [(label, value) for label, value in rows if value]


class ContactPerson(models.Model):
    """Контактное лицо клиента. Контактов может быть несколько."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField("Имя", max_length=200)
    position = models.CharField("Должность или роль", max_length=100, blank=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    telegram = models.CharField("Telegram", max_length=100, blank=True)
    max_messenger = models.CharField("Max", max_length=100, blank=True)
    email = models.EmailField("E-mail", blank=True)
    channel = models.CharField("Где общаемся", max_length=200, blank=True)
    is_main = models.BooleanField("Основной контакт", default=False)

    class Meta:
        ordering = ["-is_main", "name"]
        verbose_name = "контактное лицо"
        verbose_name_plural = "контактные лица"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_main:  # основной контакт один
            self.client.contacts.exclude(pk=self.pk).update(is_main=False)

    @property
    def channels(self):
        rows = [
            ("Телефон", self.phone),
            ("Telegram", self.telegram),
            ("Max", self.max_messenger),
            ("E-mail", self.email),
        ]
        return [(label, value) for label, value in rows if value]


class ClientNote(models.Model):
    """Заметка менеджера в карточке клиента."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    text = models.TextField("Заметка")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "заметка о клиенте"
        verbose_name_plural = "заметки о клиенте"

    def __str__(self):
        return self.text[:60]


class ClientDocument(models.Model):
    """Документ клиента: договор, счёт, акт, чек, согласие."""

    class Kind(models.TextChoices):
        CONTRACT = "contract", "Договор"
        INVOICE = "invoice", "Счёт"
        ACT = "act", "Акт или УПД"
        RECEIPT = "receipt", "Чек"
        CONSENT = "consent", "Согласие ПДн"
        OTHER = "other", "Другое"

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField("Вид", max_length=10, choices=Kind.choices, default=Kind.OTHER)
    title = models.CharField("Название", max_length=200, blank=True)
    file = models.FileField("Файл", upload_to="clients/%Y/%m/")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "документ клиента"
        verbose_name_plural = "документы клиента"

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.title or self.file.name.rsplit("/", 1)[-1]


class OrderStatus(models.Model):
    """Статус заказа. Список настраивается; по умолчанию создаётся в миграции (ROADMAP.md, «CRM»)."""

    COLORS = [
        ("--st-idle", "Синий"),
        ("--st-pause", "Жёлтый"),
        ("--st-print", "Зелёный"),
        ("--st-done", "Фиолетовый"),
        ("--st-err", "Красный"),
        ("--st-off", "Серый"),
    ]

    name = models.CharField("Название", max_length=60, unique=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    color = models.CharField("Цвет", max_length=20, choices=COLORS, default="--st-idle")
    is_final = models.BooleanField("Конечный (заказ закрыт)", default=False)
    is_side = models.BooleanField("Побочный (пауза, отмена): не входит в основную цепочку", default=False)

    class Meta:
        ordering = ["is_side", "order", "name"]
        verbose_name = "статус заказа"
        verbose_name_plural = "статусы заказа"

    def __str__(self):
        return self.name

    @classmethod
    def first(cls):
        return cls.objects.filter(is_side=False).order_by("order").first()


class Order(models.Model):
    """Заказ клиента. Принтер в заказе не выбирается: его выбирают при запуске печати."""

    number = models.CharField("Номер", max_length=20, unique=True, null=True, blank=True, editable=False)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="orders", verbose_name="Клиент")
    contact = models.ForeignKey(
        ContactPerson,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        verbose_name="Контактное лицо",
    )
    title = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)
    status = models.ForeignKey(
        OrderStatus, on_delete=models.PROTECT, related_name="orders", verbose_name="Статус"
    )
    deadline = models.DateField("Срок", null=True, blank=True)
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="responsible_orders",
        verbose_name="Ответственный",
    )
    price = models.DecimalField(
        "Цена для клиента, ₽", max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    tax_percent = models.DecimalField(
        "Налог, % от цены",
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Например, 6 для УСН «доходы». Входит в себестоимость.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "заказ"
        verbose_name_plural = "заказы"

    def __str__(self):
        return f"{self.number} {self.title}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.number:
            self.number = f"З-{self.pk:04d}"
            super().save(update_fields=["number"])

    @property
    def paid(self):
        return sum((p.amount for p in self.payments.all()), 0)

    @property
    def rest(self):
        return self.price - self.paid

    @property
    def tax_amount(self):
        return (self.price * self.tax_percent / 100).quantize(Decimal("0.01"))

    @property
    def cost_total(self):
        return sum((c.amount for c in self.cost_lines.all()), 0) + self.tax_amount

    @property
    def margin(self):
        return self.price - self.cost_total

    @property
    def margin_percent(self):
        return round(self.margin / self.price * 100) if self.price else None

    @property
    def is_overdue(self):
        return bool(self.deadline and not self.status.is_final and self.deadline < timezone.localdate())

    @property
    def checklist_progress(self):
        items = list(self.checklist.all())
        return sum(1 for i in items if i.done), len(items)


class OrderItem(models.Model):
    """Изделие в заказе. Катушка и принтер выбираются при запуске печати."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    name = models.CharField("Название", max_length=255)
    quantity = models.PositiveIntegerField("Количество", default=1, validators=[MinValueValidator(1)])
    printed = models.PositiveIntegerField("Напечатано", default=0)
    material = models.CharField("Пластик", max_length=50, blank=True)
    color = models.CharField("Цвет", max_length=50, blank=True)
    unit_price = models.DecimalField(
        "Цена за штуку, ₽", max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    plastic = models.ForeignKey(
        StockItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
        verbose_name="Пластик со склада",
    )
    print_file = models.ForeignKey(
        "OrderFile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Файл печати",
    )
    unit_weight_g = models.DecimalField(
        "Вес одной штуки, г",
        max_digits=8,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Из G-code файла печати, можно поправить вручную.",
    )
    unit_time_min = models.PositiveIntegerField(
        "Время печати одной штуки, мин",
        default=0,
        help_text="Из G-code файла печати, можно поправить вручную.",
    )

    class Meta:
        ordering = ["pk"]
        verbose_name = "изделие в заказе"
        verbose_name_plural = "изделия в заказе"

    def __str__(self):
        return self.name

    @property
    def need_grams(self):
        """Расход пластика на весь тираж."""
        return self.unit_weight_g * self.quantity

    @property
    def remaining_grams(self):
        """Резерв: пластик на ещё не напечатанные штуки. Напечатанные уже списаны со склада."""
        if self.plastic_id is None:
            return Decimal("0")
        return self.unit_weight_g * max(self.quantity - self.printed, 0)

    @property
    def total_time_min(self):
        return self.unit_time_min * self.quantity

    @property
    def shortage_grams(self):
        """Сколько граммов не хватает на складе для непечатанной части; 0 — хватает или не считается."""
        if self.plastic_id is None:
            return Decimal("0")
        available = self.plastic.available_grams(exclude_item=self)
        if available is None:
            return Decimal("0")
        return max(self.remaining_grams - available, Decimal("0"))

    @property
    def total(self):
        return self.unit_price * self.quantity

    @property
    def progress_percent(self):
        return min(100, round(self.printed / self.quantity * 100)) if self.quantity else 0


class OrderPayment(models.Model):
    """Платёж по заказу. Остаток к оплате считается: цена минус сумма платежей."""

    class Method(models.TextChoices):
        INVOICE = "invoice", "Безнал, по счёту"
        CARD = "card", "Перевод на карту"
        CASH = "cash", "Наличные"
        SELF_EMPLOYED = "npd", "Чек самозанятого"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    date = models.DateField("Дата", default=timezone.localdate)
    amount = models.DecimalField(
        "Сумма, ₽", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    method = models.CharField("Способ", max_length=10, choices=Method.choices, default=Method.INVOICE)
    document = models.CharField("Документ", max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-date", "-pk"]
        verbose_name = "платёж"
        verbose_name_plural = "платежи"

    def __str__(self):
        return f"{self.amount} ₽ · {self.date:%d.%m.%Y}"


class OrderCostLine(models.Model):
    """Статья себестоимости заказа. Налог считается отдельно по `Order.tax_percent`."""

    class Kind(models.TextChoices):
        PLASTIC = "plastic", "Пластик (стоимость изделия)"
        ELECTRICITY = "electricity", "Электричество"
        IDLE = "idle", "Простой принтера включённым"
        PLASTIC_DELIVERY = "plastic_delivery", "Доставка пластика"
        DEPRECIATION = "depreciation", "Амортизация оборудования"
        POSTPROCESS = "postprocess", "Постобработка"
        TRANSPORT = "transport", "Транспортировка заказа"
        CONSUMABLE = "consumable", "Расходник со склада"
        DEFECT = "defect", "Брак"
        OTHER = "other", "Другое"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="cost_lines")
    kind = models.CharField("Статья", max_length=20, choices=Kind.choices, default=Kind.OTHER)
    amount = models.DecimalField(
        "Сумма, ₽", max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    note = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        ordering = ["pk"]
        verbose_name = "статья себестоимости"
        verbose_name_plural = "статьи себестоимости"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.amount} ₽"


class WorkStage(models.Model):
    """Общий список этапов работ. Копируется в каждый новый заказ; менять его может только Админ."""

    name = models.CharField("Название", max_length=200, unique=True)
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "этап работ (общий список)"
        verbose_name_plural = "этапы работ (общий список)"

    def __str__(self):
        return self.name


class OrderChecklistItem(models.Model):
    """Этап работ в заказе: копия из общего списка `WorkStage` на момент создания заказа."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="checklist")
    title = models.CharField("Этап", max_length=200)
    done = models.BooleanField("Готово", default=False)

    class Meta:
        ordering = ["pk"]
        verbose_name = "этап работ"
        verbose_name_plural = "этапы работ"

    def __str__(self):
        return self.title


class OrderFile(models.Model):
    """Файл заказа (модель, чертёж, фото). Новая загрузка с тем же названием получает следующую версию."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="files")
    title = models.CharField("Название", max_length=200)
    version = models.PositiveIntegerField("Версия", default=1)
    file = models.FileField("Файл", upload_to="orders/%Y/%m/")
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title", "-version"]
        verbose_name = "файл заказа"
        verbose_name_plural = "файлы заказа"

    def __str__(self):
        return f"{self.title} v{self.version}"


class OrderEvent(models.Model):
    """Журнал заказа: смена статуса, комментарии, платежи."""

    class Kind(models.TextChoices):
        STATUS = "status", "Статус"
        COMMENT = "comment", "Комментарий"
        MONEY = "money", "Деньги"
        SYSTEM = "system", "Изменение"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SYSTEM)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "событие заказа"
        verbose_name_plural = "события заказа"

    def __str__(self):
        return self.text[:60]


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
        FILE_DELETE = "file_delete", "Файл удалён с принтера"
        FILE_RENAME = "file_rename", "Файл переименован на принтере"
        PLATE_CLEARED = "plate_cleared", "Изделие снято"
        PRINTER_LIGHT = "printer_light", "Свет принтера"
        HEATERS_OFF = "heaters_off", "Нагрев выключен"
        STOCK_GROUP_CREATE = "stock_group_create", "Группа склада создана"
        STOCK_GROUP_UPDATE = "stock_group_update", "Группа склада изменена"
        STOCK_GROUP_DELETE = "stock_group_delete", "Группа склада удалена"
        STOCK_ITEM_CREATE = "stock_item_create", "Позиция склада создана"
        STOCK_ITEM_UPDATE = "stock_item_update", "Позиция склада изменена"
        STOCK_ITEM_ARCHIVE = "stock_item_archive", "Позиция склада архивирована/восстановлена"
        CLIENT_CREATE = "client_create", "Клиент создан"
        CLIENT_UPDATE = "client_update", "Клиент изменён"
        CLIENT_ARCHIVE = "client_archive", "Клиент архивирован/восстановлен"
        CLIENT_CONTACT = "client_contact", "Контактное лицо клиента изменено"
        CLIENT_DOCUMENT = "client_document", "Документ клиента загружен/удалён"
        ORDER_CREATE = "order_create", "Заказ создан"
        ORDER_UPDATE = "order_update", "Заказ изменён"
        ORDER_STATUS = "order_status", "Статус заказа изменён"
        ORDER_ITEM = "order_item", "Изделие заказа изменено"
        ORDER_PAYMENT = "order_payment", "Платёж по заказу"
        ORDER_COST = "order_cost", "Статья себестоимости заказа"
        ORDER_FILE = "order_file", "Файл заказа загружен/удалён"
        STAGE_CATALOG = "stage_catalog", "Общий список этапов работ изменён"
        ORDER_STOCK = "order_stock", "Пластик списан под заказ"

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
