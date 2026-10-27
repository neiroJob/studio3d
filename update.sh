#!/usr/bin/env bash
# Обновление Студии 3D до последней версии из GitHub.
# Миграции БД применяются автоматически при старте контейнера web.
#   ./update.sh
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps
