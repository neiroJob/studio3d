from django import forms
from django.contrib.auth import get_user_model

from accounts.roles import has_right

from .telegram_models import TelegramRecipient, TelegramSettings


class TelegramSettingsForm(forms.ModelForm):
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
