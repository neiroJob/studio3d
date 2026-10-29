# Security Policy / Безопасность

**English** · [Русский](#русский)

## Supported versions

Studio3D is pre-1.0. Security fixes are made for the latest release on `main` only.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

- Preferred: GitHub private report — <https://github.com/neiroJob/studio3d/security/advisories/new>
  (repository → Security → "Report a vulnerability").
- Alternative: Telegram [@free_bird1987](https://t.me/free_bird1987) — describe the issue without exploit details
  first; we will agree on a private channel.

Please include the version or commit, steps to reproduce, impact, and a suggested fix if you have one. Expect a first
answer within 7 days. Please allow time for a fix before public disclosure; reporters are credited in the changelog
with their consent. There is no bug bounty.

## Security model

- Studio3D is designed for a **trusted local network**. Do not expose it directly to the internet; use a VPN for
  remote access.
- Passwords are stored as hashes (Django PBKDF2). Printer API keys are encrypted in the database with a key derived
  from `SECRET_KEY`.
- Login is throttled per IP; sessions expire; every mutating request is protected by CSRF; printer commands and
  administrative actions are written to the audit log.
- The `collector` and `web` containers connect to printers inside your network. Moonraker's own authorization
  (trusted clients / API key) still applies.
- Backups in `backups/` contain the full database — protect that folder.

In scope: Studio3D code in this repository, `docker-compose.yml`, `install.sh`, `update.sh`.
Out of scope: printer firmware (Klipper, Moonraker, vendor forks), Docker, the host OS and third-party images.

## Русский

**Не сообщайте об уязвимостях в публичных Issue.**

- Лучше всего — приватный отчёт на GitHub: <https://github.com/neiroJob/studio3d/security/advisories/new>
  (репозиторий → Security → «Report a vulnerability»).
- Или Telegram [@free_bird1987](https://t.me/free_bird1987): сначала опишите проблему без деталей эксплуатации,
  договоримся о закрытом канале.

Укажите версию или коммит, шаги воспроизведения, последствия и, если есть, предложение по исправлению. Первый ответ —
в течение 7 дней. Дайте время на исправление до публичного раскрытия; авторов отчётов с их согласия отмечаем в
CHANGELOG. Программы вознаграждений нет.

Studio3D рассчитан на **доверенную локальную сеть**. Не открывайте его напрямую в интернет — для удалённого доступа
используйте VPN. Папка `backups/` содержит полную копию базы — защищайте её.
