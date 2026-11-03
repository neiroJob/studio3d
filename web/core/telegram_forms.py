from aiogram.utils.token import TokenValidationError, validate_token
from django import forms
from django.contrib.auth import get_user_model

from accounts.roles import has_right

from .crypto import encrypt
from .telegram_models import TelegramRecipient, TelegramSettings


class TelegramSettingsForm(forms.ModelForm):
    token = forms.CharField(
        label="Токен бота от BotFather",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Введите для подключения или замены. Пустое поле сохраняет прежний токен. "
        "Токен хранится зашифрованным и после сохранения не показывается.",
    )
    remove_token = forms.BooleanField(label="Удалить сохранённый токен и отключить бота", required=False)

    def clean_token(self):
        token = self.cleaned_data["token"].strip()
        if token:
            try:
                validate_token(token)
            except TokenValidationError as error:
                raise forms.ValidationError("Некорректный формат токена BotFather.") from error
        return token

    def clean(self):
        data = super().clean()
        if data.get("token") and data.get("remove_token"):
            self.add_error("remove_token", "Выберите замену или удаление токена.")
        return data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get("remove_token"):
            instance.token_encrypted = ""
            instance.bot_started = False
        elif self.cleaned_data.get("token"):
            instance.token_encrypted = encrypt(self.cleaned_data["token"])
        if commit:
            instance.save()
        return instance

    class Meta:
        model = TelegramSettings
        fields = [
            "enabled",
            "destination",
            "interval_minutes",
            "reminder_minutes",
            "offline_minutes",
            "notify_start",
            "notify_finish",
            "notify_pause",
            "notify_errors",
            "notify_connection",
        ]


class TelegramRecipientForm(forms.ModelForm):
    class Meta:
        model = TelegramRecipient
        fields = ["name", "kind", "user", "printers", "enabled", "subscribed"]
        labels = {"user": "Пользователь проекта", "printers": "Принтеры"}
        help_texts = {
            "user": "Для личного чата выберите сотрудника с правом видеть дашборд. Для группы не требуется.",
            "enabled": "Отключение запрещает и уведомления, и команды из этого чата.",
            "subscribed": (
                "Отключает только автоматические сообщения; запросы статуса и фото остаются доступны."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")

    def clean(self):
        data = super().clean()
        if data.get("kind") == "private":
            user = data.get("user")
            if user is None or not has_right(user, "view_dashboard"):
                self.add_error("user", "Нужен активный сотрудник с правом видеть дашборд.")
        else:
            data["user"] = None
        return data
