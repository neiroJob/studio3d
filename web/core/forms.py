from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Max
from django.utils import timezone

from accounts.forms import PasswordPairMixin, password_fields

from .gcode_meta import read_gcode_meta
from .models import (
    PLASTIC_GROUP,
    Client,
    ClientDocument,
    ClientNote,
    ContactPerson,
    ElectricityTariff,
    Order,
    OrderCostLine,
    OrderFile,
    OrderItem,
    OrderPayment,
    StockGroup,
    StockItem,
    StudioSettings,
    WorkStage,
    timezone_choices,
)

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SetupForm(PasswordPairMixin, forms.Form):
    """Мастер первого запуска: админ, название студии, часовой пояс, тариф электричества."""

    first_name = forms.CharField(label="Ваше имя", max_length=150)
    username = forms.RegexField(
        label="Логин администратора",
        max_length=150,
        regex=r"^[A-Za-z0-9_.@+-]+\Z",
        error_messages={"invalid": "Только латинские буквы, цифры и символы . @ + - _"},
    )
    password1, password2 = password_fields()
    studio_name = forms.CharField(label="Название студии", max_length=200)
    timezone = forms.ChoiceField(label="Часовой пояс студии", choices=timezone_choices)
    price_kwh = forms.DecimalField(
        label="Тариф электроэнергии, ₽ за кВт·ч", max_digits=8, decimal_places=2, min_value=0
    )


class StudioSettingsForm(forms.ModelForm):
    timezone = forms.ChoiceField(label="Часовой пояс", choices=timezone_choices)

    class Meta:
        model = StudioSettings
        fields = ["name", "timezone"]


class TariffForm(forms.ModelForm):
    class Meta:
        model = ElectricityTariff
        fields = ["price_kwh", "valid_from"]
        widgets = {"valid_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["valid_from"].initial = timezone.localdate()


class StockGroupForm(forms.ModelForm):
    class Meta:
        model = StockGroup
        fields = ["name", "order"]


class StockItemForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = [
            "group",
            "name",
            "unit",
            "quantity",
            "min_quantity",
            "note",
            "purchase_price",
            "purchase_quantity",
            "supplier",
            "product_url",
            "finished_product",
        ]
        widgets = {"note": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = StockGroup.objects.all()
        self.fields["group"].empty_label = None
        self.fields["purchase_price"].required = False
        self.fields["purchase_quantity"].required = False
        if self.instance.pk and self.instance.spools.exists():
            self.fields["quantity"].disabled = True
            self.fields["unit"].disabled = True
            self.fields[
                "quantity"
            ].help_text = "Вес меняется документами и заданиями, а не прямым редактированием."

        if self.instance.pk and self.instance.output_setups.exists():
            self.fields["unit"].disabled = True
            self.fields["finished_product"].disabled = True

    def clean_purchase_price(self):
        return self.cleaned_data.get("purchase_price") or Decimal("0")

    def clean_purchase_quantity(self):
        return self.cleaned_data.get("purchase_quantity") or Decimal("1")


# --- CRM ---------------------------------------------------------------------------------------


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = [
            "kind",
            "name",
            "inn",
            "kpp",
            "ogrn",
            "address",
            "bank",
            "bik",
            "account",
            "signer",
            "tax_regime",
            "pd_consent",
            "tags",
            "next_contact",
        ]
        widgets = {"next_contact": DATE_WIDGET}


class ContactForm(forms.ModelForm):
    class Meta:
        model = ContactPerson
        fields = ["name", "position", "phone", "telegram", "max_messenger", "email", "channel", "is_main"]


class ClientNoteForm(forms.ModelForm):
    class Meta:
        model = ClientNote
        fields = ["text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 3})}


class ClientDocumentForm(forms.ModelForm):
    class Meta:
        model = ClientDocument
        fields = ["kind", "title", "file"]


class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            "client",
            "contact",
            "title",
            "description",
            "deadline",
            "responsible",
            "price",
            "tax_percent",
        ]
        widgets = {
            "deadline": DATE_WIDGET,
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client"].queryset = Client.objects.filter(is_archived=False) | Client.objects.filter(
            pk=self.instance.client_id
        )
        self.fields["client"].empty_label = None
        contacts = ContactPerson.objects.select_related("client")
        self.fields["contact"].queryset = contacts
        self.fields["contact"].label_from_instance = lambda c: f"{c.name} ({c.client.name})"
        self.fields["contact"].empty_label = "— не выбран —"
        self.fields["responsible"].queryset = get_user_model().objects.filter(is_active=True)
        self.fields["responsible"].label_from_instance = lambda u: u.first_name or u.username
        self.fields["responsible"].empty_label = "— не назначен —"

    def clean(self):
        data = super().clean()
        client, contact = data.get("client"), data.get("contact")
        if client and contact and contact.client_id != client.pk:
            self.add_error("contact", "Контактное лицо принадлежит другому клиенту.")
        return data


class OrderItemForm(forms.ModelForm):
    """Изделие заказа: пластик со склада, файл печати, время и вес одной штуки.

    Время и вес берутся из G-code файла печати, если поля оставлены пустыми; заполненные вручную
    остаются как есть.
    """

    new_file = forms.FileField(label="Или загрузить новый файл печати", required=False)

    field_order = [
        "name",
        "quantity",
        "plastic",
        "print_file",
        "new_file",
        "unit_weight_g",
        "unit_time_min",
        "unit_price",
    ]

    class Meta:
        model = OrderItem
        fields = ["name", "quantity", "plastic", "print_file", "unit_weight_g", "unit_time_min", "unit_price"]

    def __init__(self, *args, order, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.created_file = None
        self.meta_missing = False
        plastic = StockItem.objects.filter(group__name=PLASTIC_GROUP, is_archived=False)
        if self.instance.plastic_id:
            plastic = plastic | StockItem.objects.filter(pk=self.instance.plastic_id)
        self.fields["plastic"].queryset = plastic.select_related("group")
        self.fields["plastic"].label_from_instance = lambda s: f"{s.name} — остаток {s.quantity:g} {s.unit}"
        self.fields["plastic"].empty_label = "— не нужен —"
        self.fields["print_file"].queryset = order.files.all()
        self.fields["print_file"].empty_label = "— не выбран —"
        for name in ("unit_weight_g", "unit_time_min"):
            self.fields[name].required = False

    def clean(self):
        data = super().clean()
        upload, chosen = data.get("new_file"), data.get("print_file")
        weight, minutes = data.get("unit_weight_g"), data.get("unit_time_min")
        if (upload or chosen) and (not weight or not minutes):
            found_weight, found_minutes = self._read_meta(upload, chosen)
            if not weight and found_weight is not None:
                weight = found_weight
            if not minutes and found_minutes is not None:
                minutes = found_minutes
            self.meta_missing = not weight or not minutes
        data["unit_weight_g"] = weight or Decimal("0")
        data["unit_time_min"] = minutes or 0
        return data

    @staticmethod
    def _read_meta(upload, chosen):
        try:
            if upload:
                return read_gcode_meta(upload)
            with chosen.file.open("rb") as handle:
                return read_gcode_meta(handle)
        except (OSError, ValueError):
            return None, None

    def save(self, commit=True, user=None):
        item = super().save(commit=False)
        item.order = self.order
        upload = self.cleaned_data.get("new_file")
        if upload:
            last = self.order.files.filter(title=upload.name).aggregate(m=Max("version"))["m"] or 0
            self.created_file = OrderFile.objects.create(
                order=self.order, title=upload.name, version=last + 1, file=upload, uploaded_by=user
            )
            item.print_file = self.created_file
        if commit:
            item.save()
        return item


class PaymentForm(forms.ModelForm):
    class Meta:
        model = OrderPayment
        fields = ["date", "amount", "method", "document"]
        widgets = {"date": DATE_WIDGET}


class CostLineForm(forms.ModelForm):
    class Meta:
        model = OrderCostLine
        fields = ["kind", "amount", "note"]


class WorkStageForm(forms.ModelForm):
    class Meta:
        model = WorkStage
        fields = ["name"]


class OrderFileForm(forms.ModelForm):
    class Meta:
        model = OrderFile
        fields = ["title", "file", "comment"]
        labels = {"title": "Название (повтор названия создаёт новую версию)"}


class CommentForm(forms.Form):
    text = forms.CharField(label="Комментарий", widget=forms.Textarea(attrs={"rows": 2}))
