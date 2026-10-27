"""Права и базовые роли — по таблице прав из ТЗ (раздел 3).

Роль = группа Django. У одного логина может быть несколько ролей, права суммируются.
Админ может создавать свои роли и менять набор прав (кроме роли «Админ» — у неё всегда все права).
"""

from functools import wraps

from django.core.exceptions import PermissionDenied

APP = "accounts"

# (код, название) — порядок совпадает с таблицей ТЗ
RIGHTS = [
    ("view_dashboard", "Видеть дашборд и камеры"),
    ("print_control", "Пауза, отмена, продолжение печати"),
    ("print_plate", "Отметить «снял изделие / поставил на печать»"),
    ("printers_manage", "Добавлять и удалять принтеры"),
    ("orders_full", "Клиенты и заказы — полностью"),
    ("orders_anonymized", "Заказы без данных клиента и цен"),
    ("stock_income", "Склад: приход и корректировка"),
    ("stock_writeoff", "Склад: списание"),
    ("tariffs_edit", "Тарифы себестоимости: изменение"),
    ("tariffs_view", "Тарифы себестоимости: просмотр"),
    ("reports_cost", "Отчёты по себестоимости и выручке"),
    ("maint_configure", "ТО: настройка интервалов"),
    ("maint_done", "ТО: отметить выполнение"),
    ("devices_manage", "Устройства и автоматика"),
    ("integrations_manage", "Telegram, ИИ-провайдеры, расширения"),
    ("users_manage", "Пользователи и роли, журнал действий"),
    ("studio_settings", "Настройки студии и резервные копии"),
]
RIGHT_CODES = [code for code, _ in RIGHTS]

ROLE_ADMIN = "Админ"
ROLE_MANAGER = "Менеджер"
ROLE_OPERATOR = "Оператор"
BASE_ROLES = [ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR]

DEFAULT_ROLE_RIGHTS = {
    ROLE_ADMIN: RIGHT_CODES,
    ROLE_MANAGER: [
        "view_dashboard",
        "orders_full",
        "stock_income",
        "tariffs_view",
        "reports_cost",
    ],
    ROLE_OPERATOR: [
        "view_dashboard",
        "print_control",
        "print_plate",
        "orders_anonymized",
        "stock_writeoff",
        "maint_done",
    ],
}


def user_rights(user):
    """Множество кодов прав пользователя (сумма прав всех его ролей)."""
    if not user.is_authenticated or not user.is_active:
        return set()
    if not hasattr(user, "_studio_rights"):
        prefix = APP + "."
        user._studio_rights = {p[len(prefix) :] for p in user.get_all_permissions() if p.startswith(prefix)}
    return user._studio_rights


def has_right(user, code):
    return code in user_rights(user)


def right_required(*codes):
    """Декоратор вьюхи: нужен хотя бы один из перечисленных прав."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not user_rights(request.user) & set(codes):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
