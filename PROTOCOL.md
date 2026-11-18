# X16Link v1 — wire protocol between X16 apps and the x16hub

X16Link is the link that X16 Office apps (Word Processor, Calendar, …) use to
talk to the sync hub (`python -m x16hub serve`) over the TexElec serial/ESP32
card running Zimodem. It is a small, binary, request/response protocol, sized
for a 65C02 at 8 MHz with a 16-byte UART FIFO.

* Hub side: `hub/x16hub/link/` (`frame.py`, `server.py`, `docs.py`, `keys.py`).
* X16 side: `lib/x16link/` (Prog8; builds to the bank-loadable `X16LINK.BIN`).
* Reference tests: `hub/tests/test_link_*.py` (spec examples are test vectors).

All integers are **little-endian**. `str` means `[len u8][bytes]`; text on the
wire is **ISO-8859-15** (the X16 charset used by the apps), never PETSCII.

---------------------------------------------------------------------------

## 1. Transport

```
X16 app ─UART $9FE0 (115200 8N1, RTS/CTS)─ ESP32/Zimodem ─TCP─ x16hub :6502 ─HTTPS─ ownCloud
```

* **UART:** TL16C2550 port 1 at `$9FE0`, divisor 8 (921600/115200), FIFO on,
  `MCR=$23` (DTR, RTS, AFE = automatic RTS/CTS). Keep hardware flow control:
  under XON/XOFF Zimodem swallows `$11`/`$13`, so the link would not be 8-bit
  clean.
* **Dial:** `ATE0` then plain `ATD"host:port"` and wait for `CONNECT`
  (≤ 15 s). Never use the `T` (telnet: eats/doubles `$FF`), `P` (PETSCII
  translation) or `E` dial modifiers. In stream mode the link is 8-bit clean.
* **Hang up:** send `BYE`; the hub closes the TCP connection and Zimodem drops
  back to command mode (`NO CARRIER`). If that does not happen, the client
  uses the guarded escape: ≥1 s silence, `+++`, ≥1 s silence, `ATH`.
  **Zimodem forwards that `+++` to the hub first**, so the hub ignores
  `+` bytes at a frame boundary when the second byte is also `+` (a real frame
  can never start with `2B 2B`, because `$2B2B` is far above the maximum length).
* The hub listens on TCP port **6502**. The bind address comes from the hub
  config (`python -m x16hub config set bind <LAN-IP>`; default `127.0.0.1`),
  and clients outside the configured allow-list (default: private/LAN ranges
  only) are dropped before any byte is read.

## 2. Frame

```
 offset  size  field
 0       2     LEN    payload length (u16 LE)
 2       1     CHAN   channel: 0 control, 1 docs, 2 calendar, 3..255 reserved
 3       1     SEQ    request sequence number; a reply echoes its request's SEQ
 4       1     OP     operation (table below); replies are OP | $80
 5       LEN   PAYLOAD
 5+LEN   2     CRC    CRC-16/CCITT-FALSE of bytes 0 .. 4+LEN (u16 LE)
```

* **CRC-16/CCITT-FALSE:** poly `$1021`, init `$FFFF`, no reflection, xorout 0.
  Check value: CRC of ASCII `123456789` = `$29B1`.
* **Maximum payload:** each side announces the largest payload it accepts in
  `HELLO` (`maxrx`). A sender never exceeds the peer's `maxrx`, and no frame
  may exceed **1024** payload bytes (1031 bytes on the wire). Before `HELLO`
  is complete the hub accepts at most 64 payload bytes. The X16 library
  announces `maxrx = 512`.
* **Stop-and-wait:** the client sends one request and waits for its reply
  (or `ERR`). The hub never sends unsolicited frames. So at most one frame
  is in flight each way, which keeps the 16-byte FIFO + RTS/CTS sufficient
  even with polled receive.
* **SEQ:** the client increments SEQ (mod 256) for every *new* request. If a
  request arrives with the **same SEQ as the previous one**, the hub does not
  run it again; it resends its cached reply. That makes retransmission safe
  for every op (exactly-once execution).

### 2.1 Error recovery

| Receiver sees | Action |
|---|---|
| CRC mismatch | discard; skip input until the line is idle for 300 ms; hub replies `ERR BAD_CRC` (SEQ $FF); client retransmits its last request with the **same** SEQ |
| `LEN` > allowed | treat as line noise: skip until idle 300 ms, hub replies `ERR TOO_BIG` |
| reply timeout (client, default 10 s; 30 s for `COMMIT`/`OPEN`/`LIST` start) | retransmit the same request (same SEQ), at most 3 tries, then hang up |
| frame incomplete for 10 s (hub) | `ERR TIMEOUT`, close |
| 3 consecutive bad frames (hub) | close the connection |
| any error **before authentication** | hub closes the connection (no retries) |
| `ERR` reply | the request failed; the session stays usable unless the code is `AUTH_FAIL` |

## 3. Operations

| Chan | OP | Name | Direction | Purpose |
|---|---|---|---|---|
| 0 | `$01` | HELLO | C→H | version, device ID, client nonce |
| 0 | `$02` | AUTH | C→H | challenge response |
| 0 | `$03` | NOOP | C→H | keep-alive / echo |
| 0 | `$04` | TIME | C→H | local time (America/New_York) for the X16 RTC |
| 0 | `$05` | BYE | C→H | end of session, hub closes |
| 0 | `$7F` | ERR | H→C (also C→H) | error reply |
| 1 | `$10` | LIST | C→H | list ownCloud `Documents/` (paginated) |
| 1 | `$11` | OPEN | C→H | prepare a file for download |
| 1 | `$12` | READ | C→H | read a chunk of the opened file |
| 1 | `$13` | SAVE | C→H | start (or resume) an upload |
| 1 | `$14` | WRITE | C→H | upload a chunk |
| 1 | `$15` | COMMIT | C→H | verify and store the upload in ownCloud |
| 1 | `$16` | DIR | C→H | list one folder of `Documents/` (folders + files, paginated) |
| 1 | `$17` | MKDIR | C→H | create a folder |
| 1 | `$18` | DELETE | C→H | delete a file, or a folder with everything in it |
| 1 | `$19` | MOVE | C→H | move/rename a file or folder inside `Documents/` |
| 1 | `$1A` | COPY | C→H | copy a file or folder inside `Documents/` |
| 2 | `$30`–`$4F` | (calendar) | | reserved for the calendar sync ops |

Replies use `OP | $80` (e.g. `HELLO` → `$81`). Ops on any channel other than 0
are refused with `ERR BAD_STATE` until `AUTH` has succeeded.

### 3.1 ERR (`$7F`)

```
code u8, msg str, [extra…]
```

| code | name | meaning |
|---|---|---|
| 1 | BAD_FRAME | malformed frame or payload |
| 2 | BAD_CRC | frame CRC mismatch, retransmit |
| 3 | TOO_BIG | frame longer than allowed |
| 4 | BAD_OP | unknown channel/op |
| 5 | BAD_STATE | op not allowed now (not authenticated, nothing open, …) |
| 6 | AUTH_FAIL | authentication failed (hub then closes) |
| 7 | NOT_FOUND | no such file |
| 8 | CONFLICT | etag precondition failed; extra = `kind u8, etag str` (kind 1 = file already exists, 2 = file changed since you opened it). `etag` is the server's current etag, to use for an explicit overwrite |
| 9 | BAD_NAME | file name rejected (traversal, bad characters, wrong extension) |
| 10 | UNSUPPORTED | file type not supported |
| 11 | BACKEND | ownCloud unreachable / error |
| 12 | BAD_ARG | bad offset/length/size; extra = `have u32` for WRITE |
| 13 | BAD_CHECKSUM | upload size or CRC32 does not match |
| 14 | BUSY | rate limited / too many connections |
| 15 | TIMEOUT | the hub gave up waiting |
| 16 | TOO_LARGE | file above the size limit (1 MiB) |

`msg` is a short human-readable English text the app may show as is.

### 3.2 Session setup and authentication

```
C→H HELLO  (0,$01): magic "X16L", ver u8 (=1), maxrx u16, devid str, cnonce[16]
H→C HELLO' (0,$81): ver u8 (=1), maxrx u16, snonce[16], hubname str
C→H AUTH   (0,$02): ctag[32]
H→C AUTH'  (0,$82): stag[32]
```

* `devid`: 1–16 chars `[A-Za-z0-9_-]`, from the key file.
* `cnonce`: 16 bytes from the X16 (KERNAL `entropy_get` + jiffy clock).
  `snonce`: 16 bytes from the hub's CSPRNG (`secrets.token_bytes`), new on
  every connection.
* `T = devid ‖ cnonce ‖ snonce ‖ ver`
  `ctag = HMAC-SHA256(K, "X16L1C" ‖ T)`, `stag = HMAC-SHA256(K, "X16L1S" ‖ T)`.
* The hub compares `ctag` in constant time. Unknown device and wrong tag give
  the same `ERR AUTH_FAIL "authentication failed"` and a closed connection.
  More than 5 failures from one IP in 10 minutes block that IP for 10 minutes.
* The client checks `stag` too (mutual authentication: a fake hub can't feed
  documents to the X16) and hangs up on mismatch.
* **Why HMAC-SHA256 on a 6502:** an HMAC over a short message is 4 SHA-256
  compressions (~0.2 s in the Prog8 library), only once per connection, so
  there is no need for a weaker keyed hash. It is standard, and Python's `hmac`
  checks it directly.
* **Replay:** `snonce` is fresh per connection, so a recorded `ctag` is useless
  later; the direction labels `C`/`S` keep a hub tag from being reflected as a
  client tag.
* **Scope:** X16Link authenticates the peers but does **not** encrypt or MAC
  each frame (a 6502 can't afford that at line rate). Run the hub on the
  trusted LAN only. That is why the bind address and allow-list exist.
  Someone who can inject into the TCP stream or the ESP32's Wi-Fi after the
  handshake can forge requests with the device's rights (accepted risk, see
  hub/README.md). Don't put the hub behind NAT/a proxy either: all peers would
  share one address for the per-IP limits and the auth lockout.
* **Client robustness (X16 side):** the client never retransmits into a dead
  link: if a reply frame goes bad and Zimodem's `NO CARRIER` shows up in or
  right after it, that is a hang-up. It gives up after 8 frames that aren't its
  reply (wrong SEQ/op/channel), refuses READ data past the size OPEN announced,
  and requires every WRITE reply to advance `have` by exactly the chunk sent.
* **cnonce:** previous cnonce XOR fresh `entropy_get`/jiffy/VERA-scanline
  bytes, plus a per-run counter, so it never repeats within a run.

**Key K:** 32 random bytes per device. The hub keeps them in
`secrets/x16link.keys` (mode 600, dir 700, one `devid hex64` line per device),
created with `python -m x16hub link key new <devid> --sdcard <dir>`, which also
writes the X16 key file. The X16 reads `X16LINK.KEY` from the SD card (same
folder as the app):

```
HOST=192.168.1.10:6502
DEV=x16-office
KEY=<64 hex digits>
```

Each value must fill its line: `HOST` = 1–47 of `A-Z a-z 0-9 . - : _`,
`DEV` = 1–16 of `A-Z a-z 0-9 - _`, `KEY` = exactly 64 hex digits. Anything else
(a `"` or CR in HOST, a 65th hex digit, an over-long value) makes the whole file
invalid, so nothing odd can reach the modem's `ATD"..."`. Apps call `forget`
(`$A01B`) when done to wipe the key from RAM.

### 3.3 NOOP, TIME, BYE

* `NOOP ($03)`: payload ≤ 64 bytes, echoed back in `$83`.
* `TIME ($04)`: no payload. Reply `$84`:
  `year u16, month u8, day u8, hour u8, min u8, sec u8, wday u8 (0=Sunday),
  utcoff s16 (minutes, e.g. -300), dst u8, abbr str ("EST"/"EDT")`.
  Local wall-clock time in **America/New_York**; feed it to KERNAL
  `clock_set_date_time`.
* `BYE ($05)`: reply `$85` (empty), then the hub closes the connection.

### 3.4 Docs channel (1)

All documents live in ownCloud `Documents/` of the hub account (created if
missing). A **name** is the path relative to `Documents/`, `/`-separated
(e.g. `Letters/mom.rtf`), ISO-8859-15 on the wire, at most 63 bytes.

**Name rules (hub, enforced on every op):** decoded per segment; no empty,
`.` or `..` segments; no control characters, no `\ : * ? " < > |`; no
leading dot or leading/trailing spaces; ≤ 63 bytes total. `SAVE` names must end
in `.rtf`, `.txt`, `.docx`, `.xlsx` or `.csv` (case-insensitive), except with `conv 3` (raw). Anything else
is `ERR BAD_NAME`.

**Folder paths** (DIR, MKDIR, DELETE, MOVE, COPY) follow the same rules; the empty
path means `Documents/` itself and is only accepted by DIR.

**Types:** `1` = RTF (`.rtf`, the WP's native format, passed through
byte-for-byte), `2` = text (`.txt`: UTF-8 on ownCloud, ISO-8859-15 on the wire;
characters with no ISO-8859-15 form become `?`), `3` = Word (`.docx`),
`4` = Excel (`.xlsx`), `5` = CSV (`.csv`, UTF-8 on ownCloud), `0` = other
(listed; only `conv 3` can open or save it), `6` = folder (DIR entries only).

**conv** (OPEN and SAVE) says what the X16 side holds:

| conv | X16 side | valid for |
|---|---|---|
| 0 | native bytes (`.txt`/`.csv`: ISO-8859-15 ↔ UTF-8, everything else byte-for-byte) | all types |
| 1 | RTF for the X16 WP | `.rtf` `.txt` `.docx` |
| 2 | X16 sheet = "simple XLSX" (commanderx16sheets `SHEETFMT.md`) | `.xlsx` `.csv` |
| 3 | raw bytes, byte-for-byte both ways, no charset conversion (X16 file manager) | any file, any extension |

conv > 3 is `ERR BAD_ARG`; a conv that doesn't fit the file type is
`ERR UNSUPPORTED` (checked at `OPEN` / `SAVE`, before any data moves).
Conversions (hub module in brackets):

* `.docx` conv 1 [`docxconv`]: OPEN → RTF in the WP dialect (WP fonttbl, the WP's
  16-colour colortbl, `\b \i \ul \fN \cfN \highlightN`, sizes as the nearest WP size
  `\fs24/36/48/72` (12/18/24/36 pt), `\qc/\qr`, `\li` = level × 576,
  `\pagebb` for page breaks, list markers as text, tables as WP table rows
  `\trowd`..`\cellx`..`\intbl`..`\cell`..`\row` (equal widths, first row `\trhdr`; more than 8
  columns merged into the 8th, a cell's paragraphs joined with spaces), no `{\*\xwp}` tags so the
  WP re-wraps). SAVE: WP RTF → DOCX (soft-wrapped lines rejoined, list markers → real Word lists,
  fonts, text/highlight colours, alignment, indent, page breaks, tables → `w:tbl` with borders,
  the `\cellx` widths and `\trhdr` as a repeating header row). `.txt` SAVE writes a table row as
  one line, cells separated by tabs.
* `.xlsx` conv 2 [`sheetconv`]: OPEN → simple XLSX (first sheet; shared strings,
  styles mapped to the 32-entry table, booleans → 1/0, shared formulas expanded,
  X16 limits applied: 256 columns, 9999 rows, 6000 cells, 50-byte texts (cut),
  46-byte formulas (longer ones keep only their cached value)). SAVE: the upload must parse as an XLSX;
  it is re-written deflated (formulas, cached values, styles, widths kept).
* `.csv` conv 2: OPEN → simple XLSX (numbers, `12%`, `$1,234.50` recognised).
  SAVE: cell values (formula cells: cached value) → UTF-8 CSV.

Anything dropped or approximated sets the LOSSY flag on OPEN. A conversion that
fails is `ERR UNSUPPORTED` on OPEN and `ERR BAD_ARG` on COMMIT (the pending
upload is dropped). Converted data must still fit 1 MiB (`ERR TOO_LARGE`).

#### LIST (`$10`)
```
req : start u16, max u8
rep : total u16, start u16, n u8, n × entry
entry: type u8, flags u8, size u32, mtime u32 (unix UTC),
       year-2000 u8, month u8, day u8, hour u8, min u8   (local, America/New_York),
       name str
flags: bit0 unsupported (type 0, name too long or not encodable),
       bit1 name shown lossy (non ISO-8859-15 chars replaced with '?'),
       bit2 file larger than the 1 MiB limit
```
`start = 0` takes a fresh snapshot of `Documents/` (recursive, files only,
sorted case-insensitively, at most 1000 entries). Later pages read from that
snapshot, so paging is consistent. The hub packs as many entries as fit in
the client's `maxrx` (≤ `max`).

#### DIR (`$16`)
```
req : start u16, max u8, path str      path "" = Documents/ itself
rep : total u16, start u16, n u8, n × entry      (entry as in LIST)
```
Lists **one** folder (not recursive): folders first (type 6, size 0), then files, each group
sorted case-insensitively, at most 1000 entries. `name` is the entry's own name (no path).
`start = 0` takes a fresh snapshot of that folder; later pages read from it. Because any file
can be moved with `conv 3`, flags bit0 (unsupported) is set only for names that can't be sent
(too long, lossy) and files above 1 MiB, not for type 0. A missing folder, or a file given as
`path`, is `NOT_FOUND`.

#### MKDIR (`$17`), DELETE (`$18`)
```
req : path str          rep : (empty)
```
MKDIR: the parent folder must exist (`BAD_NAME "folder does not exist"` otherwise); an existing
file or folder of that name is `CONFLICT kind 1`. DELETE removes a file, or a folder **with all
its contents** (the X16 asks the user first); missing = `NOT_FOUND`. `Documents/` itself can't
be deleted (the empty path is `BAD_NAME`).

#### MOVE (`$19`), COPY (`$1A`)
```
req : mode u8, src str, dst str        rep : (empty)
```
Server-side WebDAV MOVE/COPY of a file or a whole folder; both names are full paths in
`Documents/`, so MOVE also renames. `mode 0` fails with `CONFLICT kind 1` (extra: the
destination's etag) if `dst` exists; `mode 1` replaces it. A file never replaces a folder or
the other way round (`CONFLICT` even with mode 1). `dst` equal to `src` or inside it is
`BAD_ARG`; a missing `src` is `NOT_FOUND`; a missing destination folder is `BAD_NAME`.
Transfers between the X16's SD card and ownCloud use OPEN/READ and SAVE/WRITE/COMMIT with
`conv 3`.

#### OPEN (`$11`)
```
req : conv u8, name str        conv 0 = native, 1 = RTF, 2 = simple XLSX (see table above)
rep : type u8, flags u8, size u32, crc32 u32, mtime u32, etag str
```
The hub downloads the file once and keeps the converted bytes for this
session. `crc32` (zlib/IEEE CRC-32) covers all `size` bytes as delivered.
`flags` bit1 = lossy conversion (charset or format).

#### READ (`$12`)
```
req : offset u32, len u16      (len ≤ peer maxrx − 4)
rep : offset u32, data[…]      (empty data = end of file)
```
**Resume:** after a dropped connection the client reconnects, `OPEN`s the same
name again, and if `etag` and `crc32` are unchanged it continues `READ`ing at
the offset it had reached. Otherwise it starts over.

#### SAVE (`$13`) → WRITE (`$14`)… → COMMIT (`$15`)
```
SAVE   req : mode u8, conv u8, size u32, crc32 u32, name str, etag str
       rep : have u32           bytes the hub already holds for this upload (resume)
WRITE  req : offset u32, data[…]       offset must equal `have`
       rep : have u32
COMMIT req : (empty)
       rep : etag str, mtime u32, size u32 (bytes stored in ownCloud)
```
* `mode 0` = **create only**: fails with `CONFLICT kind 1` if the name exists.
  `mode 1` = **overwrite if unchanged**: `etag` must be the etag you got from
  `OPEN` or from a `CONFLICT` reply; fails with `CONFLICT kind 2` if the file
  changed since then, or `NOT_FOUND` if it was deleted meanwhile.
  There is deliberately no unconditional overwrite: overwriting always names
  the version being replaced, so a concurrent edit is never lost silently.
* `conv 3` (raw): any name that passes the name rules, any extension; bytes stored exactly
  as sent.
* `conv 0`: bytes are stored as they are (`.txt`/`.csv`: ISO-8859-15 → UTF-8).
  `conv 1`: the content is RTF from the X16 WP. For a `.txt` target the hub
  extracts the plain text (soft-wrapped lines rejoined) and stores it as UTF-8;
  for `.docx` it converts to DOCX; for `.rtf` it is stored unchanged.
  `conv 2`: the content is an X16 sheet (simple XLSX), stored as a deflated
  `.xlsx` or as UTF-8 `.csv`.
* Uploads are buffered by the hub (≤ 1 MiB, ≤ 2 pending per device, 15 min).
  A pending upload is keyed by (device, size, crc32), so after a
  `CONFLICT` the client just sends `SAVE` again with the new mode/etag/name.
  It gets `have = size` and can `COMMIT` at once without re-sending the data.
  Same for resuming after a dropped connection.
* `COMMIT` checks `have == size` and the CRC32 (`ERR BAD_CHECKSUM` otherwise),
  then does a WebDAV `PUT` with `If-None-Match: *` (mode 0) or `If-Match`
  (mode 1). ownCloud 11 etags follow the whole-second mtime, so the hub always
  sends a strictly increasing `X-OC-MTime` and refuses to report success if the
  etag did not change.

**Conflict flow (what the WP does):**
`SAVE mode 0` → `ERR CONFLICT kind 1 etag E` → user picks *Overwrite* →
`SAVE mode 1 etag E` (have = size) → `COMMIT`; or user picks *New name* →
`SAVE mode 0 name2` → `COMMIT`.

### 3.5 Calendar channel (2)

The calendar app has no IDs and can't edit in place (see `apps/calendar/FORMAT.md`), so a sync
is **slot based**: the X16 sends all 128 slots of `CALENDAR.DAT` plus the change counter it got
from the last sync, and the hub (engine `x16hub/calsync.py`, glue `x16hub/link/cal.py`) works
out what changed on each side, pushes X16 changes to the ownCloud calendar (`x16`,
configurable: `config set calendar <name>`), and answers with the slots the X16 must
overwrite. The details (field merge, time prefix, recurring events, window) are in
`apps/calendar/SYNC-PLAN.md`.

Record = the 32-byte CALENDAR.DAT record. Text is **PETSCII in lowercase mode** (not ISO).
Flag byte bit0 = used, bit1 = edited in place, bit2 = conflict (set by the hub).

```
CAL_BEGIN     ($30) req : since u32, n u8 (≤ 128)                     rep : (empty)
CAL_PUT       ($31) req : k × (slot u8, rec[32])  (k ≥ 1)              rep : staged u8
CAL_SYNC      ($32) req : (empty)
              rep : counter u32, full u8 (bit0), m u8,
                    created u8, updated u8, deleted u8, conflicts u8, unplaced u8, unsyncable u8
CAL_GET       ($33) req : index u8, max u8                             rep : index u8, k u8, k × (slot u8, rec[32])
CAL_CONFLICTS ($34) req : start u8
              rep : total u8, start u8, k u8,
                    k × (slot u8, kind u8, field u8, kept u8, value[27] PETSCII, NUL padded)
CAL_ACK       ($35) req : k u8, k × slot u8   (k = 0: all)             rep : acknowledged u8
CAL_STATUS    ($36) req : (empty)
              rep : counter u32, x16_used u8, server_objects u16, unplaced u8, open_conflicts u8
```

* **Order:** `CAL_BEGIN since n` → `CAL_PUT` frames until `n` distinct slots are staged (the X16
  sends all 128, 15 per frame) → `CAL_SYNC` → `CAL_GET` from index 0 until `m` records are in.
  `CAL_SYNC` without a complete `CAL_BEGIN`/`CAL_PUT` set is `BAD_STATE`; a slot ≥ 128, a
  duplicate slot, a record that isn't 32 bytes, or more slots than announced is `BAD_ARG`.
  One `CAL_SYNC` per `CAL_BEGIN`.
* `since` = the counter from the last successful sync (0 = never / unknown → `full = 1` and
  `m = 128`, all slots). The hub clamps an unknown counter to 0.
* **Saving on the X16:** write the received records into the slab, save `CALENDAR.DAT`
  **first**, then the new `counter` to `CALSYNC.DAT` (4 bytes LE). If anything fails before
  that (error, timeout, hang-up), reload `CALENDAR.DAT` and keep the old counter: every sync
  is idempotent on the hub, so the next sync just repeats.
* `created/updated/deleted` = what the hub did in ownCloud for the X16's changes,
  `unplaced` = ownCloud events that didn't fit (no free slot or > 6 on a day),
  `unsyncable` = X16 records the hub couldn't use (bad date).
* **Conflicts** (both sides changed the same event since the last sync). `kind` 0 = a field was
  edited on both sides (X16 wins, `value` = the ownCloud value that lost), 1 = deleted on the
  X16 but edited in ownCloud (the edit wins and comes back to the X16), 2 = edited on the X16
  but deleted in ownCloud (X16 wins, the event is re-created). `field` 0 event, 1 date, 2 time,
  3 text; `kept` 0 = X16 version, 1 = ownCloud version; `slot` 255 = not on the X16.
  The affected records carry flag bit2 until the user acknowledges them: `CAL_ACK` (the X16
  sends it at the start of the next sync) clears the bit on that sync's pull.
* **Safety against a lost CALENDAR.DAT:** with `since = 0` (or a counter the hub doesn't
  know) an empty slot is never treated as a delete; the hub just sends everything again.
  The app also resets its counter to 0 whenever CALENDAR.DAT is missing or unreadable. A sync
  that would empty more than half of the mapped slots (and at least 10) is refused with
  `BAD_STATE "refused: would delete N events"`. If that is really intended, run
  `python -m x16hub.calsync allow-mass-delete` on the hub (one shot) and sync again.
* **One X16 per calendar:** the slot map describes one `CALENDAR.DAT`, so the first device
  that syncs owns the calendar; others get `BAD_STATE` until `python -m x16hub.calsync reset`.
* Errors: ownCloud down / sync failure → `BACKEND` (nothing applied, X16 keeps its old
  counter); the session stays usable.

## 4. Limits and timeouts (hub defaults)

| Item | Value |
|---|---|
| max payload | 1024 (client announces its own, X16 = 512) |
| pre-auth payload | 64 |
| first frame (HELLO) within | 5 s |
| handshake must finish in | 15 s |
| idle session timeout | 300 s |
| partial frame timeout | 10 s |
| connections | 4 total, 2 per address; when full, a newcomer evicts the oldest connection that has sent no HELLO for ≥ 3 s (counts as a failure for it); a connection that sent HELLO is never evicted |
| auth failures | 5 per address (and 20 per IPv6 /64) / 10 min → 10 min block. Counted: wrong key/unknown device, handshake timeout, garbage or a hang-up before AUTH (not evictions) |
| response bodies from ownCloud | read streaming, aborted above 16 MiB (files: 1 MiB) |
| file size | 1 MiB |
| LIST snapshot | 1000 entries, depth 8 |
| calendar | 128 slots, 1 sync at a time (file lock), CAL_SYNC timeout on the X16 60 s |

## 5. Worked example (hex)

`NOOP` with payload `"hi"`, SEQ 7, channel 0:

```
02 00 00 07 03 68 69 | CRC16 over those 7 bytes, LE
```

See `hub/tests/test_link_frame.py` for the exact bytes, the CRC check value,
and HELLO/AUTH vectors that also drive the X16 unit test (`lib/x16link/test`).
