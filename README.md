<p align="center">
  <img src="assets/github/logo.svg" width="440" alt="Studio3D — 3D print farm management">
</p>

<p align="center">
  <strong>English</strong> · <a href="README.ru.md">Русский</a>
</p>

<p align="center">
  Self-hosted management system for 3D printing studios and print farms: Klipper / Moonraker printers, cameras,
  G-code checks, machine hours and roles — on your local network, in Docker.
</p>

<p align="center">
  <a href="https://github.com/neiroJob/studio3d/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/neiroJob/studio3d/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: BSL 1.1" src="https://img.shields.io/badge/license-BSL%201.1-orange.svg"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776AB.svg">
  <img alt="Django 5.2" src="https://img.shields.io/badge/django-5.2-0C4B33.svg">
  <img alt="Docker Compose" src="https://img.shields.io/badge/docker-compose-2496ED.svg">
  <img alt="Klipper / Moonraker" src="https://img.shields.io/badge/Klipper-Moonraker-B12F35.svg">
</p>

Studio3D shows everything that happens in a 3D printing studio in one place: which printers are printing, what is
on the bed, how much time is left, what failed and why, and how many hours each machine has worked. It runs on your
own server inside the studio network — no cloud account, no subscription, nothing installed on the printers.

> **Status: stage 3 — printers.** Pre-1.0, under active development. Available today:
> - foundation: Docker install, first-run wizard, login with brute-force protection, roles and permissions, users,
>   electricity tariffs, audit log, daily database backups;
> - printers: adding a printer, live dashboard, printer card with camera and light, pause / resume / cancel,
>   start a file, G-code upload with progress and compatibility check, model thumbnails, job history,
>   machine hours, filament-presence reporting, and printer faults that stay visible through restarts until acknowledged.
>   Informational `// Unknown command` notices stay in the diagnostic console and are omitted from dashboard cards.
>
> Verified on real hardware: **QIDI Q1 Pro** and **Creality K1C** (Moonraker), **Creality K1** (native
> WebSocket). **QIDI Plus 4** is partially verified: connection, camera, light and file actions work; motion controls
> await a printer firmware fault being resolved. More models are added after they are tested on real printers. See
> [CHANGELOG.md](CHANGELOG.md) and [ROADMAP.md](ROADMAP.md).
>
> The user interface is in Russian today; English UI is on the roadmap.

## Telegram operator bot (`dev`)

The bot reports print start/end, pauses, faults/runout and lost/restored connections, with periodic photos
and status every N minutes during printing. It can remind operators to remove finished parts.
Private chats, groups, printer scope, events and intervals are configured under **Administration → Telegram**.
Commands provide status/remaining time, photos, recent history and machine hours. The only printer control is light on/off.

1. Open the official [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name and a unique username ending in `bot`.
2. Enter the token under **Administration → Telegram**, save settings and click **Start bot**. The token is stored encrypted and never displayed again. Replace/remove it here; an empty field keeps the existing token.
3. Install this version once with `bash update.sh` on `dev`. After installation, token changes and Start/Stop work from the admin UI without terminal commands or container restarts. The background bot service must remain running.
   No inbound port or public webhook is required. Use a dedicated bot; existing webhooks are not removed automatically.
4. Add a recipient in Administration → Telegram. For a private chat select an active project user with dashboard access;
   for a group choose the group type. Select printers or leave the selection empty for all printers.
5. Generate a single-use pairing code (10-minute expiry). The employee opens the private link and presses Start.
   For a group, add the bot with message/photo permissions and send `/pair@bot_username CODE` inside that group.
   Privacy mode can stay enabled. Group members can query status; light control additionally requires a bound private
   account with the project's print-control permission and access to that printer in both scopes.
6. Click Test, verify the message and delivery log, then enable notifications and select private/group/both delivery.
   An interval of 0 disables periodic photos or repeated removal reminders, respectively.

Use `/status`, `/status ID`, `/photo ID`, `/alerts`, `/history ID`, `/hours ID`, `/light ID on`,
`/light ID off`, `/mute`, `/unmute`, `/help`, or the printer buttons. Print start/pause/cancel/heating controls
and confirming part removal are deliberately unavailable. Revoking the project user's access revokes bot access.

Photos are uploaded by the server; Telegram never needs access to LAN camera URLs. Camera failure produces an
explicit text-only notification. The durable database queue retries network failures and Telegram rate limits;
delivery may be delayed or duplicated if a network acknowledgement is lost. The bot is a monitoring channel.
Actual Telegram delivery requires creating and configuring the bot first. K1C WebRTC frame capture still needs
verification from the installation server; it has not passed the live test from the development environment.
See the detailed [setup and troubleshooting guide (Russian)](docs/telegram.md), also explained in the admin UI.

## Screenshots

![Studio3D dashboard: six printers in different states](assets/github/dashboard.png)

<table>
  <tr>
    <td><img src="assets/github/printer.png" alt="Printer card: camera, current job, light, temperatures"></td>
    <td><img src="assets/github/printer-error.png" alt="Print error explained in plain language with Klipper messages"></td>
  </tr>
  <tr>
    <td><img src="assets/github/gcode-check.png" alt="G-code check before upload: file sliced for another printer"></td>
    <td><img src="assets/github/add-printer.png" alt="Adding a printer: model, IP address, connection check"></td>
  </tr>
</table>

<p align="center"><img src="assets/github/mobile-dark.png" width="300" alt="Dashboard on a phone, dark theme"></p>

All screenshots use synthetic demo data (`python manage.py demo_data`); they contain no real addresses or orders.

## What Studio3D does

- **Live dashboard** for the whole farm: status, progress, layer, time left, temperatures and a camera preview for
  every printer; filters by state and an "attention needed" list (errors, paused prints, parts waiting to be
  removed, heaters left on without a print).
- **Printer card**: camera, current job, grams of filament from G-code metadata, temperatures of nozzle, bed and
  chamber, case light, pause / resume / cancel, files on the printer, upload with a two-stage progress bar.
- **G-code compatibility check** before upload and start: missing start macro (e.g. `PRINT_START` on QIDI), foreign
  start macros (`START_PRINT`), tool changes on a single-extruder printer, `; printer_model` mismatch.
- **Errors in plain language**: common Klipper errors are explained, unknown commands from the file are listed,
  recent Klipper responses are shown in the card.
- **"Part removed" workflow**: after a successful print the printer waits until an operator confirms the bed is
  clear; starting a new job is blocked until then.
- **Machine hours**: print hours (history imported from the printer + jobs recorded by the system) and power-on
  hours.
- **Roles**: admin, manager, operator and custom roles; several roles per login.
- **Audit log**, **daily PostgreSQL backups** with download from the web UI, **dark and light theme**, mobile layout.
- **Extensible printer support**: a new model on a supported protocol is a YAML profile, not code.

### Roadmap highlights

CRM: customers with legal details and contacts, orders with files (3mf, G-code, STL), receipts and contracts,
kanban board, global search · price calculator and quotes, payments · stock: filament spools with automatic
consumption after each print, parts, finished goods, purchasing, QR labels · product catalog with print recipes ·
print queue and scheduler, AI failure detection · Telegram and Max bots with camera snapshots and alerts ·
statistics · order status link and request form for customers · more printer models · maintenance · MQTT devices ·
off-site backups · English UI. The order is not fixed yet. Full plan: [ROADMAP.md](ROADMAP.md).

## Supported printers

Nothing is installed on the printers; Studio3D talks to the stock firmware.

| Vendor | Model | Driver | Status |
| --- | --- | --- | --- |
| QIDI | Q1 Pro | moonraker | ✅ profile verified on a real printer |
| QIDI | Plus 4 | moonraker | ⚠️ connection, camera, light and file actions verified; motion controls pending |
| QIDI | Q2 | moonraker | planned, after testing on hardware |
| Elegoo | Neptune 4 Max | moonraker | planned, after testing on hardware |
| Creality | K1C (2025 revision) | moonraker + port 9999 for the light | ✅ verified on two printers, incl. starting prints |
| Creality | K1 | creality_native (WebSocket 9999) | ✅ verified on a real printer, incl. starting prints |
| Creality | early K1C revisions | creality_native | planned, after testing on hardware |

Any Klipper printer with Moonraker should work with a new profile — see
[adding a printer model](README.ru.md#125-новая-модель-принтера-и-тесты-драйвера) and the
[printer support request](https://github.com/neiroJob/studio3d/issues/new?template=printer_support.yml) template.

## Quick start

Requirements: Ubuntu Server 22.04 / 24.04 / 26.04 (any Linux with Docker should work), Docker Engine with the
Compose plugin, and network access from the server to the printers.

> **Give every printer a fixed IP address** (a DHCP reservation in your router is the easiest way). Studio3D
> connects to the address entered when the printer was added; if the router hands out a different one, the
> printer shows as offline.

```bash
git clone https://github.com/neiroJob/studio3d.git
cd studio3d
bash install.sh
```

`install.sh` creates `.env` with random secrets, builds and starts the containers and prints the address to open.
Open `http://<server-ip>` and complete the first-run wizard (administrator, studio name, time zone, electricity
tariff). Then use **«Добавить принтер»** (Add printer): choose the model, enter the IP address and press
**«Проверить связь»** (Check connection).

Update to the latest version:

```bash
bash update.sh
```

The detailed guide — Docker installation, backups and restore, `.env` settings, development mode, troubleshooting —
is in [README.ru.md](README.ru.md) (Russian).

## Architecture

```
  browser (LAN) ──:80──► proxy (Caddy) ──► web (Django + HTMX, gunicorn)
                                              │           │
                                              ▼           ▼
                                        db (PostgreSQL)  redis ◄── collector (asyncio) ◄──► printers
                                              ▲                     WebSocket / HTTP (Moonraker)
                                        backup (pg_dump daily)
```

| Container | Role |
| --- | --- |
| `web` | Django 5.2 + HTMX UI, roles, audit log, commands to printers |
| `collector` | Keeps a WebSocket connection to every printer, writes live state to Redis and finished jobs to PostgreSQL, reconnects automatically |
| `db` | PostgreSQL 16 |
| `redis` | Live state, login throttling, cache |
| `proxy` | Caddy on port 80, static files |
| `backup` | Daily `pg_dump` with retention |

## Security

- Designed for the studio's **local network**; no inbound access from the internet is required.
- Passwords are hashed; printer API keys are encrypted in the database.
- Login throttling per IP, CSRF protection, audit log of every printer command.
- Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## License

Studio3D is **source-available** under the [Business Source License 1.1](LICENSE). It is not an OSI-approved open
source license. In short:

- ✅ **Free** to install and run your own studio or print farm — including when your studio earns money by printing.
- ✅ Read, modify and fork the code; send improvements.
- ❌ A **commercial license** is required to sell Studio3D or a product built on it, offer it as a hosted / SaaS
  service, charge for installation or support based on it, or bundle it with hardware you sell.
- ⏳ Each version converts to the Apache License 2.0 four years after its release.

Plain-language explanation: [LICENSING.md](LICENSING.md). Commercial licensing: open an issue labelled
"commercial license" or contact Telegram [@free_bird1987](https://t.me/free_bird1987).

## Contributing

Bug reports, printer profiles and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Contributions
require agreeing to the [Contributor License Agreement](CLA.md) so the project can keep offering commercial licenses.

## Support

Questions and ideas: [GitHub Issues](https://github.com/neiroJob/studio3d/issues) · Telegram
[@free_bird1987](https://t.me/free_bird1987). See [SUPPORT.md](SUPPORT.md).

<sub>Klipper, Moonraker, QIDI, Creality, Elegoo and other names are trademarks of their respective owners; Studio3D is
not affiliated with them.</sub>
