# X16 Office sync hub

Python service that sits between Commander X16 apps (over the TexElec
UART/Zimodem link) and the suite's **ownCloud Server 11 (Classic)** at
`https://owncloud.example.lan`:

| Data | Protocol | Where on ownCloud |
|---|---|---|
| Word-processor docs, sheets (`.rtf .txt .docx .xlsx .csv`), pictures (`.bmx`, raw), any file from X16 Files (raw, folders too) | WebDAV | `/remote.php/dav/files/x16hub/Documents/` (created if missing) |
| Calendar | CalDAV | `/remote.php/dav/calendars/x16hub/x16/` (calendar "X16", created if missing) |

The X16s reach the hub over **X16Link** (`python -m x16hub serve`, TCP port 6502, protocol in
`~/x16-office/PROTOCOL.md`), dialled by the ESP32/Zimodem on the TexElec serial card.

Code: `x16hub/backend/owncloud.py` (client), `x16hub/selftest.py` (`oc check`),
`x16hub/__main__.py` (CLI), `x16hub/link/` (X16Link server: `server.py` sessions/auth/limits,
`frame.py`, `keys.py` PSKs + config, `docs.py` docs channel, `cal.py` calendar channel,
`rtfconv.py` txt<->RTF, `docxconv.py` X16 Write RTF<->DOCX (pictures too), `bmx.py` X16 BMX
pictures <-> Pillow (needs Pillow), `rtfpict.py` PNG copies of BMX pictures in `.rtf` files, `sheetconv.py` X16 Sheets simple
XLSX<->XLSX/CSV, `ooxml.py` shared zip-bomb/XML guards), `x16hub/calsync.py` (calendar sync engine). The old Google OAuth code is parked, unused, in
`x16hub/_shelved_google/`.

## Setup

```sh
cd ~/x16-office/hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## Where the credentials live

`~/x16-office/secrets/owncloud.json` (override: `$X16HUB_OC_CREDS`):

```json
{"base_url": "https://owncloud.example.lan", "username": "x16hub",
 "app_password": "<ownCloud app password>", "ca_file": "/path/to/internal-ca.pem"}
```

* File must be **600** and owned by the hub user, in a **700** directory; the hub
  refuses to run otherwise (`chmod 600 ...` hint, exit 1).
* `base_url` must be plain `https://` (no userinfo). TLS is **always** verified
  against `ca_file` (your internal CA); there is no "insecure" switch,
  proxies/`REQUESTS_CA_BUNDLE` are ignored and redirects are not followed.
* The hub authenticates with an ownCloud **app password** (not the account
  password). It never prints or logs it: `repr()` masks it, error text and log
  records are scrubbed, and transport exceptions are not chained.
* `hub.db` (data dir `hub/data/` or `$X16HUB_DATA`, 700/600) holds sync state only.
  `secrets/`, `hub/data/`, `hub/.venv/`, `*.pem`, `*.db` are git-ignored.

## Rotate the app password

1. Sign in to `https://owncloud.example.lan` as `x16hub` -> **Settings ->
   Personal -> Security -> App passwords / tokens**.
2. Create a new one named e.g. `x16hub-2026-11`. Copy it (shown once).
3. Put it in `secrets/owncloud.json` by editing the file directly in an editor
   (keeps it out of shell history and scrollback), then
   `chmod 600 ~/x16-office/secrets/owncloud.json`.
4. `.venv/bin/python -m x16hub oc check` must PASS.
5. Back in the ownCloud UI, **revoke the old app password**.

If the file may have leaked: do steps 1-5 immediately (revoking is what makes the
old one useless).

## Commands

```sh
.venv/bin/python -m x16hub oc check       # end-to-end self-test (PASS/FAIL per step), exit 0 = all pass
.venv/bin/python -m x16hub oc calendars   # calendars with ctag / sync-token
.venv/bin/python -m x16hub oc ls [X16]    # WebDAV listing with etag + lastmodified
.venv/bin/python -m pytest -q             # unit tests (no network)
```

`oc check` cleans up after itself. WebDAV: ensure `X16/`, PUT `X16/_selftest.txt`
(non-ASCII + all 256 byte values), PROPFIND, GET + byte-compare, PUT with the
current etag (accepted) then the now-stale etag (must be 412), DELETE. CalDAV:
discover calendars, ensure `x16`, check RFC 6578 support, create
`Café test ☕` at 10:00 America/New_York tomorrow, read it back via a
time-range REPORT (title + tz exact), update with If-Match (stale etag must
412), confirm the change via ctag and sync-collection, delete, confirm the
delete shows up in sync-collection. It leaves the empty `X16/` folder and the
`x16` calendar in place.

## Change detection (ownCloud 11 behaviour)

* **CalDAV:** sync-collection (RFC 6578) **is supported**; tokens look like
  `http://sabre.io/ns/sync/N` and `getctag` equals the sync token. Use
  `OwnCloud.sync_collection(cal, token)` (returns changed hrefs+etags and deleted
  hrefs); fall back to ctag + calendar-query if a token is ever rejected.
  If-Match / If-None-Match work on events.
* **WebDAV:** If-Match / If-None-Match work (412 on mismatch), **but** ownCloud
  11's file etag only changes when the whole-second mtime changes: two writes
  in the same second keep the same etag, which would let a lost update slip
  through. `OwnCloud.put()` therefore always sends `X-OC-MTime` =
  max(now, previous mtime + 1) so every write gets a new etag, and refuses
  (raises) if the server ever hands back the old etag.

## X16Link server (`serve`)

```sh
.venv/bin/python -m x16hub config show                      # bind, port, allow, hubname, calendar
.venv/bin/python -m x16hub config set bind 192.168.10.2      # LAN address the X16s' ESP32 can reach
.venv/bin/python -m x16hub config set allow 192.168.0.0/16   # who may connect (private ranges only)
.venv/bin/python -m x16hub serve                            # foreground; Ctrl+C stops it
.venv/bin/python -m x16hub serve --bind 127.0.0.1 --port 6503   # one-off override (tests)
```

* Config lives in `hub.db` (`link_config` table), not in a file. Defaults: `bind 127.0.0.1`
  (reachable only from the hub host itself, so for a real X16 set `bind` to the LAN address),
  `port 6502`, `allow` = the RFC 1918 + IPv6 ULA/link-local ranges (global networks and `/0`
  are refused), `calendar x16`.
* Logging: `X16HUB_LOG=INFO` shows every connection and sync. Secrets never appear in logs.
* Limits (PROTOCOL.md section 4): 4 connections, 2 per address, HELLO within 5 s, handshake
  15 s, idle 300 s. 5 failures (wrong key, timeout, garbage before AUTH, being evicted) in
  10 min block that address for 10 min (20 failures block a whole IPv6 /64). When all slots
  are taken, a newcomer evicts the oldest connection that has not sent HELLO for 3 s.
  A connection that has sent HELLO is never evicted. The Zimodem link is IPv4, which is the
  simplest setup. Don't run the hub behind NAT or a reverse proxy, because then every
  X16 would share one address for those limits.
* **Scope:** the handshake is mutually authenticated (HMAC-SHA256 with a per-device key),
  but later frames are not encrypted or MACed (a 6502 can't afford it). Run it on the
  trusted LAN only and keep the ESP32 on a WPA2/WPA3 Wi-Fi.

### Device keys (PSK) and the X16's SD card

One random 32-byte key per X16, in `secrets/x16link.keys` (600, in the 700 `secrets/` dir;
override `$X16HUB_LINK_KEYS`). The hub refuses group/world-accessible key files and
directories. It writes key files atomically and never follows a symlink planted at the target.

```sh
# 1. create the key and the X16's key file in a staging dir (HOST = the address the ESP32 dials)
mkdir -m 700 ~/x16-sd-staging
.venv/bin/python -m x16hub link key new x16-office --sdcard ~/x16-sd-staging --host 192.168.10.2:6502
# 2. copy ~/x16-sd-staging/X16LINK.KEY to the root of the X16's SD card (same folder as the apps),
#    e.g. card reader mounted at /media/$USER/X16:   cp ~/x16-sd-staging/X16LINK.KEY /media/$USER/X16/
#    then remove the staging copy:                    shred -u ~/x16-sd-staging/X16LINK.KEY
.venv/bin/python -m x16hub link key list                  # device ids only, never the keys
.venv/bin/python -m x16hub link key rm x16-office         # revoke (lost/stolen SD card)
```

`--sdcard` can also point straight at the mounted card. The file is plain text
(`HOST=`, `DEV=`, `KEY=`), so whoever has the card can act as that X16 towards the hub.
Running `key new` again for the same id replaces the key (the old SD file stops working).

### ESP32 / Zimodem setup on the real X16

The X16Link library dials `ATD"<HOST from X16LINK.KEY>"` itself, so no phonebook entry is
needed. For manual tests from a terminal program, and as a handy short number, add one
anyway. Use no `P`/`T` modifiers: the link must stay 8-bit clean.

```
ATW"<ssid>,<wifi password>"          join the Wi-Fi (once)
ATB115200                            115200 baud (what the library uses)
AT&K3                                RTS/CTS flow control (never XON/XOFF: it eats $11/$13)
ATP"6502000=192.168.10.2:6502,x16hub" phonebook entry -> ATD6502000 connects to the hub
AT&W                                 save
```

Check the commands against your Zimodem version's `AT` help. Test from the hub host without an
X16: `.venv/bin/python -m x16hub link ls --device x16-emu` lists `Documents/` through a
running hub with the reference client.

### Calendar sync

The calendar app's F1 key syncs over channel 2 (`x16hub/link/cal.py` -> `calsync.py`)
with the `calendar` config value (default `x16`). One X16 owns the calendar slot map: a
second device gets "calendar belongs to another X16" until you run
`.venv/bin/python -m x16hub.calsync reset` (which forgets only the hub's sync state).
`python -m x16hub.calsync status | conflicts | ack` show and clear conflicts from the hub host.
A sync that would delete more than half of the X16's events (10 or more) is refused, because
that usually means CALENDAR.DAT was lost. If it was really intended, run
`python -m x16hub.calsync allow-mass-delete` and press F1 again. `calsync dat <dir>` (sync a
CALENDAR.DAT file directly) refuses to touch state bound to a real X16. Use
`X16HUB_DATA=<other dir>` for experiments.

## Operations (production hub on the hub host)

The production hub runs as the **systemd user unit `x16hub.service`** (user `<user>`, linger on, so
it starts at boot and survives logout). Unit file: `~/.config/systemd/user/x16hub.service`.
Data dir `~/x16-office/hub/data` (700, `hub.db` 600); bind/port/allow come from `hub.db`
(`config show`), currently `bind 192.168.10.2`, `port 6502`,
`allow 127.0.0.0/8,192.168.10.2/32,192.168.1.0/24`.

```sh
systemctl --user status x16hub          # state, PID, last log lines
systemctl --user restart x16hub         # after code or config changes (config is read at start)
systemctl --user stop x16hub            # stop (start: systemctl --user start x16hub)
journalctl --user -u x16hub -f          # live log (X16HUB_LOG=INFO: connections, auth, saves, syncs)
ss -ltnp | grep 6502                    # must show 192.168.10.2:6502 only
```

* `Restart=on-failure` (5 s), so a crash or a not-yet-up LAN address at boot just retries.
* Hardening that actually applies in a user unit on this host: `NoNewPrivileges`, seccomp
  `SystemCallFilter=@system-service ~@privileged @resources`, `RestrictAddressFamilies`,
  `RestrictNamespaces`, `RestrictSUIDSGID`, `RestrictRealtime`, `LockPersonality`,
  `MemoryDenyWriteExecute`, `UMask=0077`, `MemoryMax=512M`, `TasksMax=64`.
  Ubuntu's `kernel.apparmor_restrict_unprivileged_userns=1` stops the user manager from making
  namespaces, so `ProtectSystem`/`ProtectHome`/`PrivateTmp` would be silently ignored and the
  `ProtectKernel*`/`CapabilityBoundingSet` family fails with 218/CAPABILITIES. They're left out on
  purpose. Full filesystem sandboxing needs a *system* unit with `User=<user>` (root to install).
* Tests now need the live address: the dev key file `run/sd/X16LINK.KEY` still says
  `HOST=127.0.0.1:6502`, which nothing listens on anymore. Use a private hub
  (`serve --bind 127.0.0.1 --port 6503` with a temp `X16HUB_DATA`, as `run_caltest.py` does), or a
  key file with `HOST=192.168.10.2:6502`.

### Firewall (TCP 6502)

The X16 link is authenticated but not encrypted, so allow TCP 6502 only from the network the X16's
Wi-Fi is on and drop it from everywhere else: in the host firewall, and in the hub's own allow list
(`config set allow <cidr>,...`, then restart the hub). Both layers must allow a source. With
Docker, published ports bypass ufw/firewalld and ordinary `INPUT` rules: see INSTALL.md, step 7.

### Rotate a device PSK

Each X16 has its own 32-byte key in `secrets/x16link.keys`. The production X16's id is `x16-office`,
and `x16-emu` is the emulator/test key. Rotating replaces the hub's copy, so the old SD file stops
working at once:

```sh
mkdir -m 700 -p ~/x16-sd-staging
.venv/bin/python -m x16hub link key new x16-office --sdcard ~/x16-sd-staging --host 192.168.10.2:6502
cp ~/x16-sd-staging/X16LINK.KEY /media/$USER/X16/      # the X16's SD card
shred -u ~/x16-sd-staging/X16LINK.KEY
```

No hub restart is needed: keys are read per connection. To revoke a device, run `link key rm <devid>`.
Never paste or print the key. The KEY= line is the secret.

### Tests

```sh
.venv/bin/python -m pytest -q        # 133 unit/fuzz/security-regression tests, no network
../lib/x16link/test/run_hostiletest.sh     # X16 client vs. a hostile hub (emulator)
.venv/bin/python ../apps/calendar/selftest/run_caltest.py   # calendar E2E vs. real ownCloud
../apps/wp/selftest/run_cloudtest.sh       # WP Open/Save As E2E vs. real ownCloud
```
