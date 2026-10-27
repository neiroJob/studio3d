#!/bin/sh
# Старт контейнера web: ждём БД, применяем миграции, собираем статику, запускаем команду.
set -e

python manage.py wait_for_db
python manage.py migrate --noinput
python manage.py collectstatic --noinput --verbosity 0

exec "$@"
