"""Профили моделей принтеров (YAML).

Профиль описывает модель на уже поддерживаемом протоколе: драйвер, порт, камеру, возможности.
Новая модель добавляется новым YAML-файлом без правки кода.

Поля профиля:
    id              уникальный код, латиница: qidi_q1_pro
    manufacturer    производитель, как в списке: QIDI
    model           модель: Q1 Pro
    driver          драйвер: moonraker
    verified        проверено ли на реальном принтере
    connection.port порт API по умолчанию
    connection.web_ui  адрес родного веб-интерфейса, {host} заменяется на IP
    power_w         паспортная мощность, Вт, или null
    objects.chamber имя объекта Klipper с температурой камеры, или нет поля
    camera.type     mjpeg_snapshot | none
    camera.snapshot_url  адрес снимка, {host} заменяется на IP
    capabilities    что умеет: pause, resume, cancel, start_file, upload
"""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

BUILTIN_DIR = Path(__file__).resolve().parent / "profiles"
CAPABILITIES = {"pause", "resume", "cancel", "start_file", "upload"}


@dataclass(frozen=True)
class Profile:
    id: str
    manufacturer: str
    model: str
    driver: str
    verified: bool = False
    port: int = 7125
    web_ui: str = ""
    power_w: int | None = None
    objects: dict = field(default_factory=dict)
    camera_type: str = "none"
    snapshot_url: str = ""
    capabilities: frozenset = frozenset()

    @property
    def title(self):
        return f"{self.manufacturer} {self.model}"

    def can(self, capability):
        return capability in self.capabilities

    def web_ui_for(self, host):
        return self.web_ui.format(host=host) if self.web_ui else ""

    def snapshot_url_for(self, host):
        return self.snapshot_url.format(host=host) if self.snapshot_url else ""


class ProfileError(ValueError):
    pass


def parse_profile(data, source="?"):
    try:
        caps = frozenset(data.get("capabilities") or [])
        unknown = caps - CAPABILITIES
        if unknown:
            raise ProfileError(f"{source}: неизвестные возможности {sorted(unknown)}")
        conn = data.get("connection") or {}
        cam = data.get("camera") or {}
        return Profile(
            id=str(data["id"]),
            manufacturer=str(data["manufacturer"]),
            model=str(data["model"]),
            driver=str(data["driver"]),
            verified=bool(data.get("verified", False)),
            port=int(conn.get("port", 7125)),
            web_ui=str(conn.get("web_ui") or ""),
            power_w=data.get("power_w"),
            objects=dict(data.get("objects") or {}),
            camera_type=str(cam.get("type", "none")),
            snapshot_url=str(cam.get("snapshot_url") or ""),
            capabilities=caps,
        )
    except KeyError as e:
        raise ProfileError(f"{source}: нет обязательного поля {e}") from e


@lru_cache(maxsize=1)
def all_profiles():
    profiles = {}
    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        profile = parse_profile(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)
        if profile.id in profiles:
            raise ProfileError(f"{path.name}: повторяется id {profile.id}")
        profiles[profile.id] = profile
    return profiles


def get_profile(profile_id):
    return all_profiles().get(profile_id)


def manufacturers():
    """{производитель: [профили]} для выпадающих списков."""
    result = {}
    for p in sorted(all_profiles().values(), key=lambda p: (p.manufacturer, p.model)):
        result.setdefault(p.manufacturer, []).append(p)
    return result
