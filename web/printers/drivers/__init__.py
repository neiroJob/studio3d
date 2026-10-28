"""Реестр драйверов. Новый протокол = новый модуль с Client и Monitor (ТЗ, расширения уровня «драйвер»)."""

from . import creality_native, moonraker
from .base import DriverError  # noqa: F401

DRIVERS = {
    "moonraker": (moonraker.MoonrakerClient, moonraker.MoonrakerMonitor),
    "creality_native": (creality_native.CrealityNativeClient, creality_native.CrealityNativeMonitor),
}


def client_for(printer=None, *, profile=None, host=None, port=None, api_key=""):
    profile = profile or printer.profile
    if profile is None or profile.driver not in DRIVERS:
        raise DriverError("Для этой модели нет драйвера")
    cls = DRIVERS[profile.driver][0]
    if printer is not None:
        return cls(printer.host, printer.port, printer.api_key, profile)
    return cls(host, port, api_key, profile)


def monitor_class(profile):
    return DRIVERS[profile.driver][1]
