#!/usr/bin/env python3
"""Byte-exact TCP echo + logger for the X16 UART/Zimodem loopback test.

Every received chunk is logged as hex with a timestamp, echoed back unchanged,
and the whole stream of each connection is written to <outdir>/conn<N>.bin.

Usage: echo_server.py [--host 127.0.0.1] [--port 6502] [--outdir run/]
"""
import argparse
import asyncio
import pathlib
import time

conn_no = 0


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


async def handle(reader, writer, outdir: pathlib.Path):
    global conn_no
    conn_no += 1
    n = conn_no
    peer = writer.get_extra_info("peername")
    t0 = time.monotonic()
    print(f"[conn{n}] connect from {peer}", flush=True)
    data = bytearray()
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            data += chunk
            print(f"[conn{n}] +{time.monotonic()-t0:7.3f}s rx {len(chunk):4d}: {hexdump(chunk)}", flush=True)
            writer.write(chunk)
            await writer.drain()
    except ConnectionError as e:
        print(f"[conn{n}] error {e}", flush=True)
    finally:
        (outdir / f"conn{n}.bin").write_bytes(bytes(data))
        print(f"[conn{n}] closed, {len(data)} bytes total -> conn{n}.bin", flush=True)
        writer.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6502)
    ap.add_argument("--outdir", default=".")
    a = ap.parse_args()
    outdir = pathlib.Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    srv = await asyncio.start_server(lambda r, w: handle(r, w, outdir), a.host, a.port)
    print(f"listening on {a.host}:{a.port}", flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
