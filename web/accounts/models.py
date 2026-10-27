from django.db import models

from .roles import RIGHTS


class Rights(models.Model):
    """Служебная модель без таблицы: только носитель прав системы (см. roles.RIGHTS)."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = RIGHTS
        verbose_name = "права"

    def __str__(self):
        return "права"
