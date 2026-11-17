#!/usr/bin/env python3
"""Compare what the echo server received (connN.bin) with the payload uartloop.p8 sends."""
import sys
import pathlib

PAYLOAD = bytes(range(256)) * 2 + b"+++" + bytes([0xFF, 0xFF, 0x0D, 0x0A, 0x0D, 0x00, 0x1B, 0x5B, 0x41,
                                                  0x11, 0x13, 0xFF, 0xFF, 0xFF, 0xFB, 0x01])
assert len(PAYLOAD) == 531

got = pathlib.Path(sys.argv[1]).read_bytes()
# uartloop.p8 hangs up with guard-time "+++" (then ATH); Zimodem forwards those 3 bytes
# to the remote before switching to command mode, so strip a trailing "+++" if present.
if len(got) == len(PAYLOAD) + 3 and got.endswith(b"+++"):
    print("note: trailing '+++' (hangup escape) forwarded by Zimodem - ignored")
    got = got[:-3]
print(f"expected {len(PAYLOAD)} bytes, server received {len(got)} bytes")
bad = [(i, PAYLOAD[i], got[i]) for i in range(min(len(got), len(PAYLOAD))) if got[i] != PAYLOAD[i]]
for i, e, g in bad[:20]:
    print(f"  offset {i}: sent {e:02x} got {g:02x}")
if got == PAYLOAD:
    print("SERVER SIDE: PASS - X16->network direction is byte-exact (8-bit clean)")
    sys.exit(0)
print(f"SERVER SIDE: FAIL - {len(bad)} mismatching bytes, length diff {len(got)-len(PAYLOAD)}")
sys.exit(1)
