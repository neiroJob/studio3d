from django.db import models


class MaintenanceAction(models.Model):
    name = models.CharField("Вид работы", max_length=120, unique=True)
    requires_stock = models.BooleanField("Требует списания", default=False)
    stock_group = models.ForeignKey(
        "core.StockGroup", null=True, blank=True, on_delete=models.PROTECT, verbose_name="Группа склада"
    )
    enabled = models.BooleanField("Доступно оператору", default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class MaintenanceEvent(models.Model):
    printer = models.ForeignKey(
        "printers.Printer", on_delete=models.CASCADE, related_name="maintenance_events"
    )
    request_id = models.UUIDField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    print_seconds = models.PositiveBigIntegerField()
    operator = models.CharField(max_length=150)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-recorded_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["printer", "request_id"], name="unique_maintenance_submission")
        ]

    def __str__(self):
        return f"ТО-{self.pk}"


class MaintenanceLine(models.Model):
    event = models.ForeignKey(MaintenanceEvent, on_delete=models.CASCADE, related_name="lines")
    action = models.ForeignKey(MaintenanceAction, on_delete=models.PROTECT)
    action_name = models.CharField(max_length=120)
    item_name = models.CharField(max_length=200, blank=True)
    document = models.OneToOneField("core.StockDocument", null=True, blank=True, on_delete=models.PROTECT)
    interval_seconds = models.PositiveBigIntegerField(null=True, blank=True)

    def __str__(self):
        return self.action_name
