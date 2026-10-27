#!/bin/sh
# Старт контейнера web: ждём БД, применяем миграции, собираем статику, запускаем команду.
set -e

python manage.py wait_for_db
# Миграции и статику готовит только web; коллектор стартует после него.
if [ "$1" = "gunicorn" ] || [ "$2" = "manage.py" -a "$3" = "runserver" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput --verbosity 0
fi

exec "$@"
