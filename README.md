# X16 Office Suite: sync hub + X16Link library

Office apps for the **Commander X16** (65C02, written in Prog8) that sync with an **ownCloud Server 11**
(WebDAV documents, CalDAV calendar) through a small Python hub on the LAN. The X16 reaches the
hub over a **TexElec X16 Serial & ESP32 Network Card** running Zimodem.

```
X16 app (Prog8) --X16LINK.BIN--> UART $9FE0 -> ESP32/Zimodem --TCP:6502--> x16hub --HTTPS--> ownCloud
```

| Path | What |
|---|---|
| `hub/` | `x16hub`: X16Link TCP server (`serve`), docs + calendar channels, ownCloud WebDAV/CalDAV client, calendar sync engine, CLI, tests. See `hub/README.md` |
| `lib/x16link/` | Prog8 client library for X16 apps: transport (X16LINK.BIN, RAM bank 60), docs channel (X16DOCS.BIN, bank 61), calendar client (x16cal.p8), plus emulator tests (crypto vectors, live link, hostile hub). See `lib/x16link/README.md` |
| `PROTOCOL.md` | X16Link v1 wire protocol (framing, HMAC auth, docs + calendar ops, limits) |
| `TOOLCHAIN.md` | Pinned toolchain (Prog8 12.3.5, 64tass, x16emu UART fork + libximodem commits, R49 ROM hash) and how to build it user-level |
| `Makefile`, `test/` | Toolchain build targets and the UART/Zimodem loopback test |

Apps (separate repos, each with a vendored copy of `lib/x16link`):

* **X16 Write** (word processor, Open/Save As to ownCloud): https://github.com/hermeslohin/commanderx16wp
* **Calendar** (F1 sync with a CalDAV calendar): https://github.com/hermeslohin/commanderx16calendar

The apps' end-to-end tests expect them checked out under `apps/wp` and `apps/calendar` (git-ignored here).

## Not in git

`secrets/` (ownCloud app password, device PSKs), `hub/data/` (hub.db), `run/` (logs, emulator SD
dirs, key files), `build/`, `tools/` and `src/` (rebuild them with TOOLCHAIN.md / `make`). Config
such as the ownCloud URL, CA file, bind address and allow-list lives in `secrets/owncloud.json` and
`hub.db`, never in the repo. Examples in the docs use placeholders (`owncloud.example.lan`,
`192.168.1.10`).

## Quick start

```sh
cd hub && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q              # unit, fuzz and security-regression tests (no network)
```
