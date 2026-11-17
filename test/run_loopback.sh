#!/usr/bin/env bash
# Run the UART/Zimodem 8-bit loopback test headless.
# Starts the Python echo server, boots x16emu with -uart1 ximodem, runs build/uartloop.prg,
# stops when the program prints "UARTLOOP done" (or after TIMEOUT s), then verifies the server capture.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-6502}"
TIMEOUT="${TIMEOUT:-60}"
RUN="$ROOT/run/loopback-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN" "$ROOT/run/ximodem-data"

python3 "$ROOT/test/echo_server.py" --port "$PORT" --outdir "$RUN" > "$RUN/server.log" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 1

SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
  "$ROOT/tools/x16emu.sh" -sound none -uart1 "ximodem:$ROOT/run/ximodem-data" \
  -prg "$ROOT/build/uartloop.prg" -run > "$RUN/emu.log" 2>&1 &
EMU=$!
for _ in $(seq "$TIMEOUT"); do
  grep -q "UARTLOOP done" "$RUN/emu.log" 2>/dev/null && break
  grep -q "RESULT FAIL no" "$RUN/emu.log" 2>/dev/null && break
  sleep 1
done
sleep 1
kill $EMU 2>/dev/null; wait $EMU 2>/dev/null
sleep 0.5

echo "== run dir: $RUN"
echo "== emulator console (X16 side):"; tr '\r' '\n' < "$RUN/emu.log"
echo "== server log:"; cat "$RUN/server.log"
echo "== server-side verify:"
if [ -f "$RUN/conn1.bin" ]; then python3 "$ROOT/test/verify_recv.py" "$RUN/conn1.bin"; else echo "no connection captured"; exit 1; fi
grep -q "RESULT PASS" "$RUN/emu.log"
