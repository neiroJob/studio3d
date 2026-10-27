#!/bin/sh
# Ежесуточный дамп БД в /backups (папка backups/ на сервере).
# Время запуска — BACKUP_TIME (ЧЧ:ММ, часовой пояс TZ), хранение — BACKUP_KEEP_DAYS дней.
# Ручной запуск:  docker compose exec backup /bin/sh /backup.sh now
set -eu

BACKUP_TIME="${BACKUP_TIME:-03:30}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p /backups

do_backup() {
    stamp="$(date +%Y-%m-%d_%H-%M)"
    file="/backups/studio3d_${stamp}.dump"
    echo "[backup] $(date) -> ${file}"
    if pg_dump -Fc -f "${file}.part"; then
        mv "${file}.part" "${file}"
        echo "[backup] готово: $(du -h "${file}" | cut -f1)"
    else
        rm -f "${file}.part"
        echo "[backup] ОШИБКА pg_dump" >&2
        return 1
    fi
    find /backups -maxdepth 1 -name 'studio3d_*.dump' -mtime +"${BACKUP_KEEP_DAYS}" -print -delete
}

if [ "${1:-}" = "now" ]; then
    do_backup
    exit $?
fi

echo "[backup] расписание: ежедневно в ${BACKUP_TIME}, хранить ${BACKUP_KEEP_DAYS} дн."
last=""
while true; do
    now="$(date +%H:%M)"
    today="$(date +%Y-%m-%d)"
    if [ "${now}" = "${BACKUP_TIME}" ] && [ "${last}" != "${today}" ]; then
        do_backup || true
        last="${today}"
    fi
    sleep 30
done
