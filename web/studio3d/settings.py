"""Настройки Django. Всё, что зависит от установки, берётся из переменных окружения (.env)."""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_int(name, default):
    return int(env(name, default))


DEBUG = env("DEBUG", "0") == "1"
TESTING = env("STUDIO3D_TESTING", "0") == "1" or "pytest" in sys.modules

SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "dev-only-insecure-key"
    else:
        raise ImproperlyConfigured("Задайте SECRET_KEY в .env (см. .env.example)")

# Доступ только из локальной сети, сервер открывается по IP — поэтому имя хоста не ограничиваем.
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.FirstRunMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "core.middleware.StudioTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "studio3d.urls"
WSGI_APPLICATION = "studio3d.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.studio",
            ],
        },
    },
]

# База данных: PostgreSQL в Docker; SQLite — только для тестов и разработки без Docker.
if env("DATABASE_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": env("DATABASE_HOST"),
            "PORT": env("DATABASE_PORT", "5432"),
            "NAME": env("DATABASE_NAME", "studio3d"),
            "USER": env("DATABASE_USER", "studio3d"),
            "PASSWORD": env("DATABASE_PASSWORD", ""),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

if env("REDIS_URL") and not TESTING:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": env("REDIS_URL"),
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Пароли — только хэши (стандарт Django: PBKDF2).
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Сессия: без «запомнить меня» — до закрытия браузера и не дольше SESSION_HOURS;
# с «запомнить меня» — REMEMBER_DAYS.
SESSION_HOURS = env_int("SESSION_HOURS", 12)
REMEMBER_DAYS = env_int("REMEMBER_DAYS", 30)
SESSION_COOKIE_AGE = SESSION_HOURS * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# Защита входа
LOGIN_MAX_ATTEMPTS = env_int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCK_MINUTES = env_int("LOGIN_LOCK_MINUTES", 5)

LANGUAGE_CODE = "ru"
TIME_ZONE = env("TIME_ZONE", "Europe/Moscow")  # часовой пояс по умолчанию; рабочий — из настроек студии
USE_I18N = True
USE_TZ = True  # хранение в UTC

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

BACKUP_DIR = Path(env("BACKUP_DIR", BASE_DIR / "backups"))

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
