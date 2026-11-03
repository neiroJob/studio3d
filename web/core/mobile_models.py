"""Revocable mobile sessions; only token hashes are stored."""

from django.conf import settings
from django.db import models


class MobileSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    auth_hash = models.CharField(max_length=64)
    device = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField()

    def __str__(self):
        return f"{self.user_id}: {self.device}"
