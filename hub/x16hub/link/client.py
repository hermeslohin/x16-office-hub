"""Reference X16Link client (asyncio). Used by the tests and `python -m x16hub link ...`.

Mirrors what lib/x16link does on the X16, so the hub can be checked end to end
without an emulator.
"""
from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass

from . import frame as F
from . import keys as K
from .frame import Reader, pstr


class LinkError(Exception):
    def __init__(self, code: int, msg: str, extra: bytes = b""):
        super().__init__(f"ERR {code}: {msg}")
        self.code, self.msg, self.extra = code, msg, extra


@dataclass
class ListEntry:
    type: int
    flags: int
    size: int
    mtime: int
    local: tuple
    name: str


class Client:
    def __init__(self, reader, writer, maxrx: int = 512):
        self.r, self.w, self.maxrx = reader, writer, maxrx
        self.seq = 0
        self.hub_maxrx = 64

    @classmethod
    async def connect(cls, host: str, port: int, devid: str, key: bytes, maxrx: int = 512) -> "Client":
        r, w = await asyncio.open_connection(host, port)
        c = cls(r, w, maxrx)
        await c.handshake(devid, key)
        return c

    async def recv(self, timeout: float = 30) -> F.Frame:
        hdr = await asyncio.wait_for(self.r.readexactly(2), timeout)
        (n,) = struct.unpack("<H", hdr)
        rest = await asyncio.wait_for(self.r.readexactly(3 + n + 2), timeout)
        fr, _ = F.decode(hdr + rest, F.ABS_MAX_PAYLOAD)
        return fr

    async def request(self, chan: int, op: int, payload: bytes = b"", *, resend: bool = False) -> bytes:
        if not resend:
            self.seq = (self.seq + 1) & 0xFF
        self.w.write(F.encode(chan, self.seq, op, payload))
        await self.w.drain()
        fr = await self.recv()
        if fr.op == F.OP_ERR:
            r = Reader(fr.payload)
            code, msg = r.u8(), r.str_().decode("iso8859_15")
            raise LinkError(code, msg, r.rest())
        if fr.op != op | F.REPLY or fr.seq != self.seq:
            raise LinkError(0, f"unexpected reply {fr.name} seq {fr.seq}")
        return fr.payload

    async def handshake(self, devid: str, key: bytes) -> None:
        cnonce = os.urandom(16)
        p = await self.request(F.CH_CTRL, F.OP_HELLO, F.MAGIC + bytes([F.VERSION]) + struct.pack("<H", self.maxrx)
                               + pstr(devid.encode()) + cnonce)
        r = Reader(p)
        _ver, self.hub_maxrx, snonce, _name = r.u8(), r.u16(), r.raw(16), r.str_()
        t = K.transcript(devid.encode(), cnonce, snonce, F.VERSION)
        stag = await self.request(F.CH_CTRL, F.OP_AUTH, K.client_tag(key, t))
        if stag != K.server_tag(key, t):
            raise LinkError(F.E_AUTH_FAIL, "hub failed mutual authentication")

    async def close(self) -> None:
        try:
            await self.request(F.CH_CTRL, F.OP_BYE)
        except (LinkError, asyncio.IncompleteReadError, ConnectionError):
            pass
        self.w.close()

    # ---- control
    async def noop(self, data: bytes = b"") -> bytes:
        return await self.request(F.CH_CTRL, F.OP_NOOP, data)

    async def time(self) -> dict:
        r = Reader(await self.request(F.CH_CTRL, F.OP_TIME))
        y, mo, d, h, mi, s, wd = r.u16(), r.u8(), r.u8(), r.u8(), r.u8(), r.u8(), r.u8()
        off = struct.unpack("<h", r.raw(2))[0]
        dst, abbr = r.u8(), r.str_().decode()
        return dict(year=y, month=mo, day=d, hour=h, minute=mi, second=s, wday=wd, utcoff=off, dst=dst, abbr=abbr)

    # ---- docs
    async def list(self, page: int = 20) -> tuple[int, list[ListEntry]]:
        out: list[ListEntry] = []
        start, total = 0, None
        while total is None or start < total:
            r = Reader(await self.request(F.CH_DOCS, F.OP_LIST, struct.pack("<HB", start, page)))
            total, _st, n = r.u16(), r.u16(), r.u8()
            for _ in range(n):
                t, fl, size, mt = r.u8(), r.u8(), r.u32(), r.u32()
                loc = tuple(r.raw(5))
                out.append(ListEntry(t, fl, size, mt, loc, r.str_().decode("iso8859_15")))
            r.end()
            if n == 0:
                break
            start += n
        return total or 0, out

    async def dir(self, path: str = "", page: int = 255) -> tuple[int, list[ListEntry]]:
        """DIR: one folder of Documents/ (folders first, type 6), all pages."""
        out: list[ListEntry] = []
        start, total = 0, None
        while total is None or start < total:
            r = Reader(await self.request(F.CH_DOCS, F.OP_DIR, struct.pack("<HB", start, page) + pstr(path)))
            total, _st, n = r.u16(), r.u16(), r.u8()
            for _ in range(n):
                t, fl, size, mt = r.u8(), r.u8(), r.u32(), r.u32()
                loc = tuple(r.raw(5))
                out.append(ListEntry(t, fl, size, mt, loc, r.str_().decode("iso8859_15")))
            r.end()
            if n == 0:
                break
            start += n
        return total or 0, out

    async def mkdir(self, path: str) -> None:
        await self.request(F.CH_DOCS, F.OP_MKDIR, pstr(path))

    async def delete(self, path: str) -> None:
        await self.request(F.CH_DOCS, F.OP_DELETE, pstr(path))

    async def move(self, src: str, dst: str, mode: int = 0) -> None:
        await self.request(F.CH_DOCS, F.OP_MOVE, bytes([mode]) + pstr(src) + pstr(dst))

    async def copy(self, src: str, dst: str, mode: int = 0) -> None:
        await self.request(F.CH_DOCS, F.OP_COPY, bytes([mode]) + pstr(src) + pstr(dst))

    async def open(self, name: str, conv: int = 0) -> dict:
        r = Reader(await self.request(F.CH_DOCS, F.OP_OPEN, bytes([conv]) + pstr(name)))
        t, fl, size, crc, mt = r.u8(), r.u8(), r.u32(), r.u32(), r.u32()
        return dict(type=t, flags=fl, size=size, crc=crc, mtime=mt, etag=r.str_().decode())

    async def read_all(self, size: int, start: int = 0) -> bytes:
        buf = bytearray()
        off = start
        chunk = min(self.maxrx, 1024) - 4
        while off < size:
            p = await self.request(F.CH_DOCS, F.OP_READ, struct.pack("<IH", off, chunk))
            (o,) = struct.unpack_from("<I", p)
            data = p[4:]
            if o != off or not data:
                raise LinkError(0, "short read")
            buf += data
            off += len(data)
        return bytes(buf)

    async def get(self, name: str, conv: int = 0) -> tuple[dict, bytes]:
        info = await self.open(name, conv)
        data = await self.read_all(info["size"])
        if F.crc32(data) != info["crc"]:
            raise LinkError(F.E_BAD_CHECKSUM, "crc32 mismatch")
        return info, data

    async def save(self, name: str, data: bytes, *, mode: int = 0, etag: str = "", conv: int = 0,
                   stop_after: int | None = None) -> dict:
        p = await self.request(F.CH_DOCS, F.OP_SAVE, bytes([mode, conv]) + struct.pack("<II", len(data), F.crc32(data))
                               + pstr(name) + pstr(etag.encode()))
        (have,) = struct.unpack("<I", p)
        chunk = min(self.hub_maxrx, 1024) - 4
        sent = 0
        while have < len(data):
            if stop_after is not None and sent >= stop_after:
                return {"have": have}
            p = await self.request(F.CH_DOCS, F.OP_WRITE, struct.pack("<I", have) + data[have:have + chunk])
            (have,) = struct.unpack("<I", p)
            sent += 1
        r = Reader(await self.request(F.CH_DOCS, F.OP_COMMIT))
        etag2 = r.str_().decode()
        mt, size = r.u32(), r.u32()
        return {"etag": etag2, "mtime": mt, "size": size, "have": have}

    # ---- calendar channel (PROTOCOL.md 3.5)
    async def cal_sync(self, records: list[tuple[int, bytes]], counter: int) -> dict:
        """Act like the X16 Sync button: stage records, sync, fetch changed slots."""
        await self.request(F.CH_CAL, F.OP_CAL_BEGIN, struct.pack("<IB", counter, len(records)))
        per = max(1, min(self.hub_maxrx, 512) // 33)
        for i in range(0, len(records), per):
            await self.request(F.CH_CAL, F.OP_CAL_PUT,
                               b"".join(bytes([s]) + bytes(r) for s, r in records[i:i + per]))
        r = Reader(await self.request(F.CH_CAL, F.OP_CAL_SYNC))
        res = {"counter": r.u32(), "full": bool(r.u8() & 1), "m": r.u8(), "created": r.u8(),
               "updated": r.u8(), "deleted": r.u8(), "conflicts": r.u8(), "unplaced": r.u8(),
               "unsyncable": r.u8(), "records": []}
        while len(res["records"]) < res["m"]:
            g = Reader(await self.request(F.CH_CAL, F.OP_CAL_GET, bytes([len(res["records"]), 255])))
            g.u8()
            k = g.u8()
            if k == 0:
                raise LinkError(0, "CAL_GET returned nothing")
            for _ in range(k):
                res["records"].append((g.u8(), g.raw(32)))
        return res

    async def cal_conflicts(self) -> list[dict]:
        out, total = [], 1
        while len(out) < total:
            r = Reader(await self.request(F.CH_CAL, F.OP_CAL_CONFLICTS, bytes([len(out)])))
            total, _, k = r.u8(), r.u8(), r.u8()
            if k == 0:
                break
            for _ in range(k):
                out.append({"slot": r.u8(), "kind": r.u8(), "field": r.u8(), "kept": r.u8(),
                            "value": r.raw(27).rstrip(b"\0")})
        return out

    async def cal_ack(self, slots: list[int] | None = None) -> int:
        s = slots or []
        return (await self.request(F.CH_CAL, F.OP_CAL_ACK, bytes([len(s)] + s)))[0]

    async def cal_status(self) -> dict:
        c, used, srv, unpl, conf = struct.unpack("<IBHBB", await self.request(F.CH_CAL, F.OP_CAL_STATUS))
        return {"counter": c, "used": used, "server": srv, "unplaced": unpl, "conflicts": conf}
