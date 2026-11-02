from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.forms import PasswordPairMixin, password_fields

from .models import (
    Client,
    ClientDocument,
    ClientNote,
    ContactPerson,
    ElectricityTariff,
    Order,
    OrderChecklistItem,
    OrderCostLine,
    OrderFile,
    OrderItem,
    OrderPayment,
    StockGroup,
    StockItem,
    StudioSettings,
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
        fields = ["group", "name", "unit", "quantity", "min_quantity", "note"]
        widgets = {"note": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = StockGroup.objects.all()
        self.fields["group"].empty_label = None


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
    class Meta:
        model = OrderItem
        fields = ["name", "quantity", "material", "color", "unit_price"]


class PaymentForm(forms.ModelForm):
    class Meta:
        model = OrderPayment
        fields = ["date", "amount", "method", "document"]
        widgets = {"date": DATE_WIDGET}


class CostLineForm(forms.ModelForm):
    class Meta:
        model = OrderCostLine
        fields = ["kind", "amount", "note"]


class ChecklistForm(forms.ModelForm):
    class Meta:
        model = OrderChecklistItem
        fields = ["title"]


class OrderFileForm(forms.ModelForm):
    class Meta:
        model = OrderFile
        fields = ["title", "file", "comment"]
        labels = {"title": "Название (повтор названия создаёт новую версию)"}


class CommentForm(forms.Form):
    text = forms.CharField(label="Комментарий", widget=forms.Textarea(attrs={"rows": 2}))
