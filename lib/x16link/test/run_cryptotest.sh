#!/usr/bin/env bash
# Build + run the X16 crypto self-test headless and compare every line with Python's hashlib/hmac/zlib.
set -eu
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
B="$ROOT/build/x16link"
mkdir -p "$B"
"$ROOT/tools/prog8c" -target cx16 -srcdirs "$ROOT/lib/x16link" -out "$B" "$ROOT/lib/x16link/test/cryptotest.p8" > "$B/cryptotest.build.log" 2>&1 || { cat "$B/cryptotest.build.log"; exit 1; }
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy timeout 60 "$ROOT/tools/x16emu.sh" -sound none -prg "$B/cryptotest.prg" -run > "$B/cryptotest.out" 2>&1 &
EMU=$!
for _ in $(seq 60); do grep -q "CRYPTOTEST done" "$B/cryptotest.out" 2>/dev/null && break; sleep 1; done
kill $EMU 2>/dev/null; wait $EMU 2>/dev/null || true
python3 - "$B/cryptotest.out" <<'EOF'
import sys, hashlib, hmac, zlib
out = open(sys.argv[1], "rb").read().decode("latin-1").replace("\r", "\n")
got = {}
for line in out.splitlines():
    parts = line.split()
    if len(parts) == 2 and parts[0] in ("CRC16","CRC32","SHA_ABC","SHA_200","SHA_55","SHA_56","HMAC","HMAC_JIFFIES"):
        got[parts[0]] = parts[1]
def crc16(d, c=0xFFFF):
    for b in d:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c
m = bytes(range(200))
exp = {
    "CRC16": "%04x" % crc16(b"123456789"),
    "CRC32": (zlib.crc32(b"123456789") & 0xFFFFFFFF).to_bytes(4, "little").hex(),
    "SHA_ABC": hashlib.sha256(b"abc").hexdigest(),
    "SHA_200": hashlib.sha256(m).hexdigest(),
    "SHA_55": hashlib.sha256(m[:55]).hexdigest(),
    "SHA_56": hashlib.sha256(m[:56]).hexdigest(),
    "HMAC": hmac.new(bytes(range(32)), b"X16L1Cx16-test" + m[:32] + b"\x01", hashlib.sha256).hexdigest(),
}
bad = 0
for k, v in exp.items():
    ok = got.get(k) == v
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} {k:8} x16={got.get(k)} py={v if not ok else '(same)'}")
print(f"HMAC time on the X16: {got.get('HMAC_JIFFIES')} jiffies (1/60 s)")
print("RESULT", "PASS" if bad == 0 else f"FAIL ({bad})")
sys.exit(1 if bad else 0)
EOF
