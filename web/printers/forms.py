from django import forms

from .models import Printer
from .profiles import all_profiles


class PrinterForm(forms.ModelForm):
    """Добавление и настройки принтера (ТЗ, раздел 4: «Добавление принтера»)."""

    profile_id = forms.ChoiceField(label="Модель")
    api_key = forms.CharField(
        label="API-ключ",
        required=False,
        widget=forms.PasswordInput(render_value=True),
        help_text="Только если на принтере включена авторизация Moonraker.",
    )
    print_hours_base = forms.DecimalField(
        label="Часы печати до подключения",
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=1,
        help_text="Подставляется из истории принтера при проверке связи.",
    )

    class Meta:
        model = Printer
        fields = [
            "profile_id",
            "name",
            "host",
            "port",
            "zone",
            "power_w",
            "purchase_cost",
            "amortization_hours",
            "enabled",
        ]
        widgets = {
            "host": forms.TextInput(attrs={"placeholder": "192.168.1.50", "inputmode": "decimal"}),
            "name": forms.TextInput(attrs={"placeholder": "например, Q1Pro-01"}),
            "zone": forms.TextInput(attrs={"placeholder": "Зал печати"}),
        }
        help_texts = {
            "power_w": "Средняя мощность при печати — для расчёта электроэнергии.",
            "amortization_hours": "За сколько часов печати принтер должен окупиться.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile_id"].choices = [("", "—")] + [
            (p.id, p.title) for p in sorted(all_profiles().values(), key=lambda p: p.title)
        ]
        self.fields["port"].required = False
        self.fields["port"].help_text = "Пусто — порт из профиля модели."
        self.fields["host"].help_text = "Лучше закрепить за принтером постоянный IP в роутере."
        if self.instance.pk:
            self.fields["api_key"].initial = self.instance.api_key
            self.fields["print_hours_base"].initial = round(self.instance.print_seconds_base / 3600, 1)
            self.fields["print_hours_base"].help_text = "Наработка до подключения к системе."
        else:
            self.fields.pop("enabled")

    def clean_host(self):
        host = self.cleaned_data["host"].strip()
        for prefix in ("http://", "https://"):
            host = host.removeprefix(prefix)
        return host.rstrip("/").split("/")[0].split(":")[0]

    def clean(self):
        data = super().clean()
        profile = all_profiles().get(data.get("profile_id"))
        if profile and not data.get("port"):
            data["port"] = profile.port
        return data

    def save(self, commit=True):
        printer = super().save(commit=False)
        printer.port = self.cleaned_data["port"]
        printer.api_key = self.cleaned_data.get("api_key", "")
        hours = self.cleaned_data.get("print_hours_base")
        if hours is not None:
            printer.print_seconds_base = int(hours * 3600)
        if commit:
            printer.save()
        return printer


class UploadForm(forms.Form):
    file = forms.FileField(label="Файл G-code")
    start = forms.BooleanField(label="Сразу запустить печать", required=False)

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".gcode", ".gco", ".g")):
            raise forms.ValidationError("Нужен файл G-code (.gcode).")
        return f
