from django import forms
from django.utils import timezone

from accounts.forms import PasswordPairMixin, password_fields

from .models import ElectricityTariff, StudioSettings, timezone_choices


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
