#!/usr/bin/env bash
# X16Link client vs. a hostile hub + bad key files (security regression, W7 review F6-F8, F10).
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
B="$ROOT/build/x16link"
SD="$ROOT/run/sd-hostile"
PORT=6504
mkdir -p "$B" "$SD" "$ROOT/run/ximodem-data"
P8="$ROOT/tools/prog8c -target cx16 -srcdirs $ROOT/lib/x16link -out $B"
for f in x16link x16docs; do $P8 "$ROOT/lib/x16link/$f.p8" > "$B/$f.build.log" 2>&1 || { cat "$B/$f.build.log"; exit 1; }; done
$P8 "$ROOT/lib/x16link/test/hostiletest.p8" > "$B/hostile.build.log" 2>&1 || { cat "$B/hostile.build.log"; exit 1; }
cp "${LIBS:-$B}/x16link.bin" "$SD/X16LINK.BIN"; cp "${LIBS:-$B}/x16docs.bin" "$SD/X16DOCS.BIN"; cp "$B/hostiletest.prg" "$SD/HOSTILE.PRG"   # LIBS=dir: test other builds
umask 077
DEV=$(grep -o '^DEV=.*' "$ROOT/run/sd/X16LINK.KEY" | cut -d= -f2)
sed "s/^HOST=.*/HOST=127.0.0.1:$PORT/" "$ROOT/run/sd/X16LINK.KEY" > "$SD/X16LINK.KEY"
K=$(grep -v '^HOST=' "$SD/X16LINK.KEY")
printf 'HOST=127.0.0.1.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:6502\n%s\n' "$K" > "$SD/KEYBAD1.KEY"
printf 'HOST=127.0.0.1:6502"\rATH\n%s\n' "$K" > "$SD/KEYBAD2.KEY"
sed 's/^\(KEY=.*\)$/\1ab/' "$SD/X16LINK.KEY" > "$SD/KEYBAD3.KEY"
sed 's/^DEV=.*/DEV=abcdefghijklmnopq/' "$SD/X16LINK.KEY" > "$SD/KEYBAD4.KEY"
"$ROOT/hub/.venv/bin/python" "$ROOT/lib/x16link/test/evil_hub.py" $PORT "$DEV" "$SD/X16LINK.KEY" > "$B/evil_hub.log" 2>&1 &
HUB=$!
sleep 1
cd "$SD"
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy "$ROOT/tools/x16emu.sh" -sound none \
  -uart1 "ximodem:$ROOT/run/ximodem-data" -prg HOSTILE.PRG -run > "$B/hostile.out" 2>&1 &
EMU=$!
for _ in $(seq "${TIMEOUT:-180}"); do grep -q "HOSTILE done" "$B/hostile.out" 2>/dev/null && break; sleep 1; done
kill $EMU $HUB 2>/dev/null; wait $EMU $HUB 2>/dev/null
rm -f "$SD"/KEYBAD*.KEY "$SD"/DL-EVIL.TMP
OUT=$(tr '\r' '\n' < "$B/hostile.out" | grep -a -E '^[A-Z_]+ st=')
echo "$OUT"
fail=0
want() { echo "$OUT" | grep -qx "$1 st=$2" && echo "PASS $1 -> $2 ($3)" || { echo "FAIL $1 expected st=$2 ($3)"; fail=1; }; }
want KEY_LONGHOST 32 "over-long HOST refused"
want KEY_QUOTEHOST 32 "quote/CR in HOST refused"
want KEY_LONGKEY 32 "66 hex digits refused"
want KEY_LONGDEV 32 "17-char DEV refused"
want KEY_GOOD 0 "valid key file"
want NOCARRIER_MIDFRAME 41 "NO CARRIER inside a frame = hang-up, no retransmit"
want JUNK_FRAMES 38 "wrong-SEQ flood gives up"
want READ_PAST_SIZE 38 "data past announced size rejected"
want WRITE_HAVE_STUCK 38 "non-advancing have rejected"
n=$(grep -c '^RX NOOP' "$B/evil_hub.log")
[ "$n" = 2 ] && echo "PASS hub saw each NOOP once (no retransmits)" || { echo "FAIL hub saw $n NOOP frames"; fail=1; }
echo "RESULT $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
