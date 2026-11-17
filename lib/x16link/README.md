# x16link: X16Link client library for Commander X16 apps (Prog8)

The X16 side of the link between X16 Office apps and the sync hub (`~/x16-office/hub`),
which talks to ownCloud. The wire protocol is in `~/x16-office/PROTOCOL.md`.

Hardware: TexElec X16 Serial & ESP32 Network Card. Port 1 ($9FE0) is the ESP32 running
**Zimodem**, at 115200 baud with RTS/CTS. The library dials `ATD"host:port"` (plain, 8-bit
clean TCP) and hangs up with the guarded `+++` / `ATH`. In the emulator,
`x16emu -uart1 ximodem:<dir>` behaves the same way.

## Pieces

| File | Built as | What it does |
|---|---|---|
| `x16link.p8` (+ `lk_link`, `lk_uart`, `lk_crypto`, `lk_util`) | `X16LINK.BIN`, RAM bank **60** | Transport: key file, dial, HMAC-SHA256 mutual auth, sets the RTC from the hub's TIME, framed request/reply with retransmit |
| `x16docs.p8` (+ `lk_docs`) | `X16DOCS.BIN`, RAM bank **61** | Docs channel: LIST with page cache and picker rows, download (OPEN+READ to an SD file, CRC-32, resume), upload (SAVE+WRITE+COMMIT, resume, conflict info) |
| `x16cal.p8` | compiled **into the app** | Calendar channel: `lkcal.sync(events, counter)` for the calendar's 128-slot CALENDAR.DAT, plus the conflict list and acknowledgements |
| `x16link_api.p8` | app side | `lkload.load()` loads both banks from the SD card. `lkload.load_link()` loads only X16LINK.BIN (the calendar uses this) |
| `x16link_ext.p8` | app side | `lkapi.*` extsub declarations of both jump tables |

Build both bank libraries (the WP script does this too):

```sh
P8="tools/prog8c -target cx16 -srcdirs lib/x16link -out build/x16link"
$P8 lib/x16link/x16link.p8 && $P8 lib/x16link/x16docs.p8
# app: add  -srcdirs ~/x16-office/lib/x16link  and  %import x16link_api  (+ %import x16cal)
```

`X16LINK.BIN` is full: about 25 bytes are left in its 8 KB bank. Put new code in the app
or in another bank, not in the transport.

## Memory rules for apps

* Leave golden RAM **$0400-$07FF** alone. It holds `XB` (the payload buffer,
  $0400-$0607), the SHA/HMAC scratch (inside XB, $0500-$05FF, used only while connecting)
  and the UART receive ring ($0700-$07FF). Callers may use $0608-$06FF (X16 Write's
  cloud bank uses $0608-$06F3).
* Don't use RAM banks 60/61. X16 Write also uses bank 62 for `X16WPCL.BIN`.
* Strings and buffers passed to the libraries must be in main RAM or golden RAM, never
  in banked RAM.

## API (`lkapi`, status in A: 0 = ok, 1-16 = hub error codes, 32+ = local)

| Call | |
|---|---|
| `load_key(name)` | Parse `X16LINK.KEY` (see below). 32 = missing or invalid |
| `connect()` | Dial, HELLO/AUTH (the hub must prove it knows the key too), TIME→RTC |
| `request(mkword(op, chan), plen, timeout)` | Payload in XB; reply in XB, `reply_len()` bytes |
| `hangup()` / `forget()` | BYE + modem hangup / wipe the key from RAM (call it when you're done) |
| `link_error(dst, max)` | Last error text (ISO-8859-1) |
| `list_begin/list_total/list_row/list_name` | Docs picker (X16DOCS.BIN) |
| `download(remote, local, conv)` / `upload(local, remote, mode, conv)` | Docs transfer. `conv 1` = RTF on the X16 side |
| `get_etag/set_etag/conflict` | Overwrite-if-unchanged and the conflict kind/etag after status 8 |

Local statuses: 32 no/bad key file, 33 no serial card, 34 no modem, 35 can't reach the hub,
36 timeout, 37 the hub failed authentication, 38 protocol error (also: a hostile or broken
peer), 39 SD error, 40 CRC-32 mismatch, 41 link dropped, 42 bad argument.

Calendar (`lkcal`, `%import x16cal`): `sync(events, &counter)` → status. On success, save
CALENDAR.DAT, *then* store `lkcal.new_counter` (4 bytes) in CALSYNC.DAT. On failure, reload
CALENDAR.DAT and keep the old counter. Results: `created/updated/deleted` (sent),
`received`, `nconf` + `conf_slot/kind/field/kept/text`, `unplaced`. `queue_ack(slot)` /
`ack_all()` are sent at the start of the next sync. `get_error(dst, max)` gives the
error text (ISO).

## Key file `X16LINK.KEY` (SD card, next to the app)

```
HOST=192.168.1.10:6502
DEV=x16-office
KEY=<64 hex digits>
```

Create it on the hub with `python -m x16hub link key new <devid> --sdcard <dir> --host
<hub-ip>:6502` (see hub/README.md). Each value must fill its line: HOST is 1-47 of
`A-Z a-z 0-9 . - : _`, DEV is 1-16 of `A-Z a-z 0-9 - _`, KEY is exactly 64 hex digits.
Anything else rejects the whole file, so nothing can reach the modem's `ATD"..."` command.
The file's text is wiped from RAM after parsing. Treat the SD card like a house key:
anyone with the file can act as this X16 towards the hub (revoke with `link key rm`).

## Tests

| Script | Checks |
|---|---|
| `test/run_cryptotest.sh` | CRC-16/CRC-32/SHA-256/HMAC against Python (no network) |
| `test/run_linktest.sh` | List/download/upload/conflict against the hub on 127.0.0.1:6502 and real ownCloud (needs `LKTEST.*` seeded, see the script) |
| `test/run_hostiletest.sh` | Security regression: bad key files, a hostile hub (`test/evil_hub.py`) sending NO CARRIER mid-frame, wrong-SEQ floods, READ past the size, and a WRITE `have` that doesn't advance. All 10 checks fail on the pre-W7 libraries and pass now |
| `apps/calendar/selftest/run_caltest.py` | Calendar sync end to end (see apps/calendar) |
