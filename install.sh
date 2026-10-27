#!/usr/bin/env bash
# Первая установка Студии 3D на Ubuntu Server.
# Создаёт .env с случайными секретами (если его ещё нет) и запускает контейнеры.
#   ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker не найден. Установите Docker по README, раздел «Подготовка сервера»." >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "Плагин docker compose не найден. Установите docker-compose-plugin." >&2
    exit 1
fi

if [ -f .env ]; then
    echo ".env уже есть — секреты не трогаю."
else
    secret="$(openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-64)"
    dbpass="$(openssl rand -hex 24)"
    sed -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" \
        -e "s|^DATABASE_PASSWORD=.*|DATABASE_PASSWORD=${dbpass}|" \
        .env.example > .env
    chmod 600 .env
    echo "Создан .env со случайными SECRET_KEY и DATABASE_PASSWORD."
fi

mkdir -p backups
docker compose up -d --build

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
port="$(grep -E '^HTTP_PORT=' .env | cut -d= -f2)"
url="http://${ip:-<IP сервера>}"
[ -n "${port}" ] && [ "${port}" != "80" ] && url="${url}:${port}"
echo
echo "Готово. Откройте ${url} и пройдите мастер первого запуска."
