#!/usr/bin/env python3
"""A hostile X16Link hub for hostiletest.p8: authenticates properly (it has the test device key), then
misbehaves. NOOP "S2": sends 2 bytes of a frame header and hangs up. NOOP "S3": answers with endless
valid frames carrying the wrong SEQ. OPEN: says 10 bytes, then READ returns 300. SAVE: have=0 and every
WRITE answers have=0. Logs what it received (one line per frame) to stdout."""
import asyncio
import secrets
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hub"))
from x16hub.link import frame as F  # noqa: E402
from x16hub.link import keys as K  # noqa: E402

DEV, KEY = sys.argv[2], bytes.fromhex(Path(sys.argv[3]).read_text().split("KEY=")[1][:64])


async def rd(r):
    hdr = await r.readexactly(2)
    n = hdr[0] | hdr[1] << 8
    rest = await r.readexactly(n + 5)
    fr, _ = F.decode(hdr + rest)
    print("RX", F.OP_NAMES.get(fr.op, hex(fr.op)), fr.seq, fr.payload[:8].hex(), flush=True)
    return fr


async def handle(r, w):
    try:
        h = await rd(r)
        p = h.payload
        cn = p[-16:]
        sn = secrets.token_bytes(16)
        w.write(F.encode(0, h.seq, 0x81, bytes([1]) + struct.pack("<H", 1024) + sn + F.pstr(b"evil")))
        a = await rd(r)
        t = K.transcript(DEV.encode(), cn, sn, 1)
        assert a.payload == K.client_tag(KEY, t)
        w.write(F.encode(0, a.seq, 0x82, K.server_tag(KEY, t)))
        while True:
            fr = await rd(r)
            if fr.op == F.OP_NOOP and fr.payload == b"S2":
                w.write(b"\x05\x00")
                await w.drain()
                w.close()
                return
            if fr.op == F.OP_NOOP and fr.payload == b"S3":
                for _ in range(40):
                    w.write(F.encode(0, (fr.seq + 7) & 0xFF, 0x83, b"no"))
                    await w.drain()
                    await asyncio.sleep(0.05)
                continue
            if fr.op == F.OP_TIME:
                from x16hub.link.server import time_payload
                w.write(F.encode(0, fr.seq, 0x84, time_payload()))
            elif fr.op == F.OP_OPEN:
                w.write(F.encode(1, fr.seq, 0x91, bytes([1, 0]) + struct.pack("<III", 10, 0, 0) + F.pstr(b"e")))
            elif fr.op == F.OP_READ:
                off = struct.unpack("<I", fr.payload[:4])[0]
                w.write(F.encode(1, fr.seq, 0x92, struct.pack("<I", off) + b"A" * 300))
            elif fr.op == F.OP_SAVE:
                w.write(F.encode(1, fr.seq, 0x93, struct.pack("<I", 0)))
            elif fr.op == F.OP_WRITE:
                w.write(F.encode(1, fr.seq, 0x94, struct.pack("<I", 0)))
            elif fr.op == F.OP_BYE:
                w.write(F.encode(0, fr.seq, 0x85))
                await w.drain()
                w.close()
                return
            await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        print("CLOSED", flush=True)
        w.close()


async def main():
    s = await asyncio.start_server(handle, "127.0.0.1", int(sys.argv[1]))
    async with s:
        await s.serve_forever()

asyncio.run(main())
