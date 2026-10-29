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
    camera.type     mjpeg_snapshot | webrtc_iframe | none
    camera.snapshot_url  адрес снимка (для mjpeg_snapshot), {host} заменяется на IP
    camera.page_url      адрес страницы со встроенным видео (для webrtc_iframe) — когда
                    принтер отдаёт только WebRTC-страницу без отдельного снимка, она
                    показывается во фрейме на странице принтера (без миниатюры на плитке
                    дашборда и без записи в таймлапс — это не снимок, а чужая страница)
    capabilities    что умеет: pause, resume, cancel, start_file, upload, light, delete_file, rename_file
    light.object    объект Klipper с состоянием света (поле value > 0 — включён)
    light.on_gcode / light.off_gcode  G-code включения и выключения света
                    (не «on/off»: в YAML это читается как true/false)
    light.native_port  вместо object/on_gcode/off_gcode — свет не через Klipper/Moonraker,
                    а короткой командой нативного WebSocket-протокола Creality (lightSw)
                    на этот порт того же хоста (у K1C свет не виден через Moonraker,
                    только так). Состояние света при этом не известно вживую — показывается
                    последняя отправленная команда (Printer.light_hint)
    tools           число экструдеров (по умолчанию 1)
    gcode.start_macro   стартовый макрос, который должен быть в начале файла: PRINT_START
    gcode.model_names   как слайсеры пишут эту модель в «; printer_model = …»
    power_loss_recovery  путь файла восстановления после отключения питания на самом принтере
                    (например, у QIDI — ".plr/plr.gcode", прошивка сама пишет его при
                    отключении питания во время печати). Если он появился в списке файлов —
                    на странице принтера предлагается запустить его как обычный файл. ❓ не
                    проверено на реальном сценарии потери питания — только то, что принтер
                    создаёт такую папку (память проекта, printers.md)
"""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

BUILTIN_DIR = Path(__file__).resolve().parent / "profiles"
CAPABILITIES = {"pause", "resume", "cancel", "start_file", "upload", "light", "delete_file", "rename_file"}


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
    page_url: str = ""
    capabilities: frozenset = frozenset()
    light_object: str = ""
    light_on: str = ""
    light_off: str = ""
    light_native_port: int | None = None
    power_loss_recovery: str = ""
    start_macro: str = ""
    model_names: tuple = ()
    tools: int = 1

    @property
    def title(self):
        return f"{self.manufacturer} {self.model}"

    def can(self, capability):
        return capability in self.capabilities

    def web_ui_for(self, host):
        return self.web_ui.format(host=host) if self.web_ui else ""

    def snapshot_url_for(self, host):
        return self.snapshot_url.format(host=host) if self.snapshot_url else ""

    def page_url_for(self, host):
        return self.page_url.format(host=host) if self.page_url else ""


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
        gcode = data.get("gcode") or {}
        light = data.get("light") or {}
        light_native_port = light.get("native_port")
        if "light" in caps and not light_native_port:
            if not (light.get("object") and light.get("on_gcode") and light.get("off_gcode")):
                raise ProfileError(
                    f"{source}: для возможности light нужны light.native_port, "
                    "либо light.object, light.on_gcode и light.off_gcode"
                )
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
            page_url=str(cam.get("page_url") or ""),
            capabilities=caps,
            light_object=str(light.get("object") or ""),
            light_on=str(light.get("on_gcode") or ""),
            light_off=str(light.get("off_gcode") or ""),
            light_native_port=int(light_native_port) if light_native_port else None,
            power_loss_recovery=str(data.get("power_loss_recovery") or ""),
            start_macro=str(gcode.get("start_macro") or ""),
            model_names=tuple(str(n) for n in (gcode.get("model_names") or [])),
            tools=int(data.get("tools", 1)),
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
