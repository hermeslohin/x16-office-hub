#!/usr/bin/env bash
# Build the X16Link bank libraries + linktest.prg and run it headless (x16emu -uart1 ximodem) against
# the hub on 127.0.0.1:6502. The emulator's SD card is run/sd (needs X16LINK.KEY:
#   hub/.venv/bin/python -m x16hub link key new x16-emu --sdcard run/sd --host 127.0.0.1:6502).
set -eu
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
B="$ROOT/build/x16link"
SD="${SD:-$ROOT/run/sd}"
TIMEOUT="${TIMEOUT:-240}"
mkdir -p "$B" "$SD" "$ROOT/run/ximodem-data"
P8="$ROOT/tools/prog8c -target cx16 -srcdirs $ROOT/lib/x16link -out $B"
for f in x16link x16docs; do $P8 "$ROOT/lib/x16link/$f.p8" > "$B/$f.build.log" 2>&1 || { cat "$B/$f.build.log"; exit 1; }; done
$P8 "$ROOT/lib/x16link/test/linktest.p8" > "$B/linktest.build.log" 2>&1 || { cat "$B/linktest.build.log"; exit 1; }
cp "$B/x16link.bin" "$SD/X16LINK.BIN"
cp "$B/x16docs.bin" "$SD/X16DOCS.BIN"
cp "$B/linktest.prg" "$SD/LINKTEST.PRG"
rm -f "$SD"/DL-*.TMP
cd "$SD"
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy "$ROOT/tools/x16emu.sh" -sound none \
  -uart1 "ximodem:$ROOT/run/ximodem-data" -prg LINKTEST.PRG -run > "$B/linktest.out" 2>&1 &
EMU=$!
for _ in $(seq "$TIMEOUT"); do grep -q "LINKTEST done" "$B/linktest.out" 2>/dev/null && break; sleep 1; done
kill $EMU 2>/dev/null; wait $EMU 2>/dev/null || true
tr '\r' '\n' < "$B/linktest.out" | grep -a -E "^(KEY|CONNECT|LIST|ROW|DOWNLOAD|UPLOAD|ETAG|CONFLICT|HANGUP|LINKTEST)"
