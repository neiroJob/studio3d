"""Шифрование токенов и ключей в БД (ТЗ, раздел 14). Ключ выводится из SECRET_KEY."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    digest = hashlib.sha256(("studio3d:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
