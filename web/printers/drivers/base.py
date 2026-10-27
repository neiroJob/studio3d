"""Единый интерфейс драйверов и единая модель состояния принтера (ТЗ, раздел 4).

Состояние — обычный dict, его пишет коллектор и читает веб:

    {
      "status": "offline|ready|printing|paused|error",   # «завершено» и «обслуживание» добавляет веб
      "message": "текст ошибки или пусто",
      "temps": {"nozzle": [тек, цель], "bed": [тек, цель], "chamber": [тек, цель] | None},
      "job": None | {
          "filename": str, "progress": 0..1, "layer": int|None, "total_layers": int|None,
          "elapsed": сек, "remaining": сек|None, "filament_mm": float, "filament_g_total": float|None,
      },
      "updated": unix time,
    }
"""

OFFLINE = "offline"
READY = "ready"
PRINTING = "printing"
PAUSED = "paused"
ERROR = "error"
FINISHED = "finished"  # завершено, ждёт снятия — признак хранится в БД
MAINTENANCE = "maintenance"  # обслуживание — появится на этапе ТО

STATUS_LABELS = {
    OFFLINE: "Нет связи",
    READY: "Готов",
    PRINTING: "Печать",
    PAUSED: "Пауза",
    ERROR: "Ошибка",
    FINISHED: "Ждёт снятия",
    MAINTENANCE: "Обслуживание",
}


class DriverError(Exception):
    """Принтер не ответил или ответил ошибкой. Текст — для пользователя."""


def offline_state(message="", updated=0.0):
    return {"status": OFFLINE, "message": message, "temps": {}, "job": None, "updated": updated}


class BaseClient:
    """Синхронные запросы к принтеру из веба: проверка связи, команды, файлы."""

    def __init__(self, host, port, api_key="", profile=None, timeout=8):
        self.host = host
        self.port = port
        self.api_key = api_key
        self.profile = profile
        self.timeout = timeout

    def check(self):
        """Проверка связи. Возвращает dict с версиями и найденной камерой или бросает DriverError."""
        raise NotImplementedError

    def history_totals(self):
        """Наработка по истории принтера: {"print_seconds": float}."""
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def resume(self):
        raise NotImplementedError

    def cancel(self):
        raise NotImplementedError

    def list_files(self):
        """[{"path": str, "size": int, "modified": unix time}] — новые сверху."""
        raise NotImplementedError

    def start(self, filename):
        raise NotImplementedError

    def upload(self, fileobj, filename, start=False):
        raise NotImplementedError


class BaseMonitor:
    """Постоянное соединение с принтером в коллекторе.

    run() работает, пока соединение живо: вызывает on_state(state) при каждом изменении
    и on_job(job) при завершении задания. При обрыве — бросает исключение, коллектор переподключится.
    """

    def __init__(self, host, port, api_key="", profile=None):
        self.host = host
        self.port = port
        self.api_key = api_key
        self.profile = profile

    async def run(self, on_state, on_job, history_since=0.0):
        raise NotImplementedError
