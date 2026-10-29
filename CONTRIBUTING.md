# Contributing / Как участвовать

**English** · [Русский](#русский)

Thank you for helping Studio3D. The most valuable contributions right now:

- **Printer profiles and test data** for models you own (see below) — this is how new printers get supported.
- **Bug reports** with clear reproduction steps.
- **Pull requests** for issues labelled `good first issue` or discussed in an issue first.

## Before you start

- For anything larger than a small fix, open an issue first and describe the idea — it saves time for both sides.
- Read the [Contributor License Agreement](CLA.md). Every pull request must tick the CLA checkbox: Studio3D is also
  offered under commercial licenses, and the CLA lets the project include your contribution there.
- Never commit secrets, real IP addresses of your network, customer data, real order files or private G-code.

## Development setup

```bash
git clone https://github.com/neiroJob/studio3d.git
cd studio3d/web
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check . && ruff format --check .
```

Tests use SQLite and a fake Moonraker server (`web/tests/fake_moonraker.py`); no printer, Docker or PostgreSQL is
needed. Full stack with hot reload: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`.

Demo data for exploring the UI on a test database: `python manage.py demo_data --hold 600`.

## Adding a printer model

1. Record read-only responses of your printer (nothing is sent to the printer):
   ```bash
   python3 tools/moonraker_dump.py <printer-ip> > web/tests/fixtures/<model>_idle.json
   ```
   Remove file names that reveal customer projects before committing.
2. Add `web/printers/profiles/<vendor>_<model>.yaml` based on `qidi_q1_pro.yaml`. Fields are documented in
   `web/printers/profiles.py`. Take `gcode.start_macro` from the printer's `printer.cfg`.
3. Add tests that load your fixture, like the `REAL` tests in `web/tests/test_printers.py`.
4. Mark the profile `verified: true` only if you checked it on a real printer.

## Pull request checklist

- Tests and `ruff` pass; new behaviour has tests.
- `CHANGELOG.md` has an entry under "Не выпущено / Unreleased".
- UI changes: screenshots in the PR; if README screenshots change, regenerate them with `tools/screenshots.py`
  on demo data.
- Line endings are LF (`.gitattributes` enforces it).
- Commit messages are short and in Russian or English.

## Code of conduct

Be respectful. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## Русский

Спасибо за помощь Studio3D. Сейчас нужнее всего:

- **Профили принтеров и тестовые данные** для моделей, которые у вас есть (ниже) — так появляется поддержка новых
  принтеров.
- **Сообщения об ошибках** с понятными шагами воспроизведения.
- **Pull request** по задачам с меткой `good first issue` или после обсуждения в Issue.

### Перед началом

- Для всего крупнее мелкой правки сначала откройте Issue и опишите идею.
- Прочитайте [соглашение участника (CLA)](CLA.md). В каждом pull request нужно отметить пункт CLA: Studio3D
  распространяется и по коммерческим лицензиям, и CLA позволяет включить туда ваш вклад.
- Не коммитьте секреты, реальные IP вашей сети, данные клиентов, реальные файлы заказов и закрытый G-code.

### Окружение для разработки

Команды — в английской части выше. Тесты работают на SQLite и поддельном Moonraker, принтер, Docker и PostgreSQL не
нужны. Подробности — [README.ru.md](README.ru.md), раздел 12.

### Новая модель принтера

1. Запишите ответы принтера скриптом `tools/moonraker_dump.py` (только чтение). Перед коммитом уберите имена
   файлов, по которым видны проекты клиентов.
2. Добавьте профиль `web/printers/profiles/<производитель>_<модель>.yaml` по образцу `qidi_q1_pro.yaml`.
   `gcode.start_macro` возьмите из `printer.cfg` принтера.
3. Добавьте тесты на записанных данных, как тесты `REAL` в `web/tests/test_printers.py`.
4. `verified: true` — только если проверено на реальном принтере.

### Чек-лист pull request

- Тесты и `ruff` проходят, у нового поведения есть тесты.
- В `CHANGELOG.md` есть запись в разделе «Не выпущено».
- Для изменений интерфейса — скриншоты в PR.
- Окончания строк LF.
