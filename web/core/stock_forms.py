from django import forms
from django.core.exceptions import ValidationError

from .models import OrderItem, StockItem
from .stock_models import PrinterStockSetup, PrintStockRun, Spool


class SpoolForm(forms.ModelForm):
    opening = forms.BooleanField(
        label="Распределить старый остаток (не создавать повторный приход)", required=False
    )

    class Meta:
        model = Spool
        fields = [
            "item",
            "code",
            "nominal_g",
            "remaining_g",
            "tare_g",
            "purchase_price",
            "location",
            "status",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = StockItem.objects.filter(is_archived=False, finished_product=False)
        self.fields["nominal_g"].help_text = "Только пластик, без тары: например, 750 или 1000 г."

    def clean(self):
        data = super().clean()
        item = data.get("item")
        if item and not item.grams_per_unit:
            self.add_error(
                "item",
                "Откройте Склад → Остатки → Изменить у этого материала. "
                "Выберите единицу «Граммы (г)» и пересчитайте остаток в граммы. "
                "Затем вернитесь к приходу катушки.",
            )
        if data.get("remaining_g") is not None and data.get("nominal_g") is not None:
            if data["remaining_g"] > data["nominal_g"]:
                self.add_error("remaining_g", "Остаток не может превышать номинальный вес.")
        return data


class StockSetupForm(forms.ModelForm):
    class Meta:
        model = PrinterStockSetup
        fields = ["spool", "order_item", "output_item", "output_quantity"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["spool"].queryset = Spool.objects.filter(item__is_archived=False, remaining_g__gt=0)
        self.fields["order_item"].queryset = OrderItem.objects.filter(order__status__is_final=False)
        self.fields["output_item"].queryset = StockItem.objects.filter(
            is_archived=False, finished_product=True, unit="шт"
        )

    def clean(self):
        data = super().clean()
        if (
            self.instance.printer_id
            and PrintStockRun.objects.filter(printer_id=self.instance.printer_id, state="active").exists()
        ):
            raise ValidationError("Во время печати нельзя менять привязку катушки и изделия.")
        if self.instance.printer_id:
            from printers.state import read_state

            state = read_state(self.instance.printer_id) or {}
            if state.get("status") in ("printing", "paused"):
                raise ValidationError("Настройте катушку и выпуск до запуска печати, на свободном принтере.")
        if data.get("order_item") and data.get("spool"):
            if data["order_item"].plastic_id != data["spool"].item_id:
                self.add_error("order_item", "Материал заказа должен совпадать с материалом катушки.")
        if data.get("order_item") and not data.get("output_item"):
            self.add_error("output_item", "Выберите готовое изделие для оприходования после снятия.")
        return data


class MovementForm(forms.Form):
    kind = forms.ChoiceField(
        label="Операция",
        choices=[
            ("income", "Приход"),
            ("writeoff", "Списание"),
            ("count", "Инвентаризация: фактический остаток"),
        ],
    )
    item = forms.ModelChoiceField(label="Позиция", queryset=StockItem.objects.filter(is_archived=False))
    spool = forms.ModelChoiceField(
        label="Катушка (если пластик)", queryset=Spool.objects.all(), required=False
    )
    quantity = forms.DecimalField(
        label="Количество в единицах позиции (для катушки — г)", min_value=0, max_digits=10, decimal_places=2
    )
    reason = forms.CharField(label="Основание / причина", max_length=255)


class CorrectionForm(forms.Form):
    weight = forms.DecimalField(
        label="Уточнённый суммарный расход задания, г", min_value=0, max_digits=10, decimal_places=2
    )
    reason = forms.CharField(label="Причина корректировки", max_length=180)
