"""Создание базовых ролей после миграций."""

from django.contrib.auth.management import create_permissions
from django.contrib.auth.models import Group, Permission

from .roles import APP, DEFAULT_ROLE_RIGHTS, RIGHT_CODES, ROLE_ADMIN


def ensure_roles(sender=None, app_config=None, using="default", **kwargs):
    app_config = app_config or sender
    create_permissions(app_config, verbosity=0, using=using)
    perms = {p.codename: p for p in Permission.objects.using(using).filter(content_type__app_label=APP)}

    for role, codes in DEFAULT_ROLE_RIGHTS.items():
        group, created = Group.objects.using(using).get_or_create(name=role)
        # Базовые роли получают права по умолчанию только при создании — дальше их настраивает админ.
        # Роль «Админ» всегда имеет все права.
        if created or role == ROLE_ADMIN:
            group.permissions.set([perms[c] for c in codes if c in perms])

    # Права, которых больше нет в коде, удаляем.
    Permission.objects.using(using).filter(content_type__app_label=APP).exclude(
        codename__in=RIGHT_CODES
    ).delete()
