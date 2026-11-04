"""Складские документы и автоматический учёт печати (в существующем core)."""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Spool(models.Model):
    item = models.ForeignKey(
        "core.StockItem", on_delete=models.PROTECT, related_name="spools", verbose_name="Материал"
    )
    code = models.CharField("Номер катушки", max_length=80, unique=True)
    nominal_g = models.DecimalField(
        "Номинальный вес пластика, г",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    remaining_g = models.DecimalField(
        "Остаток пластика, г", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    tare_g = models.DecimalField(
        "Вес пустой катушки, г", max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    purchase_price = models.DecimalField(
        "Закупочная цена катушки, ₽",
        max_digits=12,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    location = models.CharField("Место хранения", max_length=100, blank=True)
    status = models.CharField(
        "Состояние",
        max_length=20,
        choices=[
            ("sealed", "Запечатана"),
            ("open", "Открыта"),
            ("drying", "На сушке"),
            ("empty", "Израсходована"),
        ],
        default="sealed",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.CheckConstraint(condition=Q(remaining_g__gte=0), name="spool_nonnegative")]

    def __str__(self):
        return f"{self.code} · {self.item.name} · {self.remaining_g} г"


class PrinterStockSetup(models.Model):
    printer = models.OneToOneField("printers.Printer", on_delete=models.CASCADE, related_name="stock_setup")
    spool = models.OneToOneField(
        Spool,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="installed_on",
        verbose_name="Установленная катушка",
    )
    order_item = models.ForeignKey(
        "core.OrderItem", null=True, blank=True, on_delete=models.PROTECT, verbose_name="Изделие заказа"
    )
    output_item = models.ForeignKey(
        "core.StockItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="output_setups",
        verbose_name="Готовое изделие на складе",
    )
    output_quantity = models.PositiveIntegerField(
        "Изделий на одном столе", default=1, validators=[MinValueValidator(1)]
    )

    def __str__(self):
        return f"Катушка и выпуск: {self.printer}"


class PrintStockRun(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Печатается / резерв"
        PENDING = "pending", "Требует проверки расхода"
        SETTLED = "settled", "Расход учтён"
        VOID = "void", "Запуск не состоялся"

    printer = models.ForeignKey("printers.Printer", on_delete=models.PROTECT, related_name="stock_runs")
    job = models.OneToOneField(
        "printers.PrintJob", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_run"
    )
    spool = models.ForeignKey(Spool, on_delete=models.PROTECT, related_name="print_runs")
    order_item = models.ForeignKey(
        "core.OrderItem", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_runs"
    )
    output_item = models.ForeignKey(
        "core.StockItem", null=True, blank=True, on_delete=models.PROTECT, related_name="production_runs"
    )
    output_quantity = models.PositiveIntegerField(default=1)
    filename = models.CharField(max_length=500)
    started_at = models.DateTimeField()
    estimated_g = models.DecimalField(
        "Вес из слайсера, г", max_digits=10, decimal_places=2, null=True, blank=True
    )
    estimated_seconds = models.FloatField("Время из слайсера, с", null=True, blank=True)
    active_seconds = models.FloatField("Учтённое время печати без нагрева/пауз, с", default=0)
    last_elapsed = models.FloatField(null=True)
    last_printing = models.BooleanField(default=False)
    last_observed_at = models.DateTimeField(null=True)
    timing_complete = models.BooleanField(default=True)
    consumed_g = models.DecimalField("Списанный вес, г", max_digits=10, decimal_places=2, default=0)
    price_per_g = models.DecimalField(
        "Цена грамма на момент запуска", max_digits=14, decimal_places=6, default=0
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    issue = models.CharField("Требует внимания", max_length=255, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    corrected = models.BooleanField(default=False)
    settled_status = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["printer"], condition=Q(state="active"), name="one_active_stock_run"
            )
        ]

    def __str__(self):
        return f"{self.printer} · {self.filename}"

    @property
    def reserved_g(self):
        return (self.estimated_g or Decimal("0")) if self.state == self.State.ACTIVE else Decimal("0")

    @property
    def material_cost(self):
        return (self.consumed_g * self.price_per_g).quantize(Decimal("0.01"))


class StockMovement(models.Model):
    item = models.ForeignKey("core.StockItem", on_delete=models.PROTECT, related_name="movements")
    spool = models.ForeignKey(
        Spool, null=True, blank=True, on_delete=models.PROTECT, related_name="movements"
    )
    run = models.ForeignKey(
        PrintStockRun, null=True, blank=True, on_delete=models.PROTECT, related_name="movements"
    )
    quantity = models.DecimalField("Изменение остатка в единицах позиции", max_digits=12, decimal_places=2)
    unit = models.CharField(max_length=20, blank=True)
    kind = models.CharField(
        max_length=20,
        choices=[
            ("income", "Приход"),
            ("writeoff", "Списание"),
            ("count", "Инвентаризация"),
            ("print", "Расход печати"),
            ("correction", "Корректировка расхода"),
            ("output", "Снятое изделие"),
        ],
    )
    reason = models.CharField("Основание", max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk}: {self.item}"


class StockDocument(models.Model):
    """Черновик хранит ввод; остаток меняется только при проведении."""

    item = models.ForeignKey("core.StockItem", on_delete=models.PROTECT)
    spool = models.ForeignKey(Spool, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=20, choices=[("income", "Приход"), ("writeoff", "Списание"), ("count", "Инвентаризация")]
    )
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit = models.CharField(max_length=20, blank=True)
    reason = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    movement = models.OneToOneField(StockMovement, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk}"
