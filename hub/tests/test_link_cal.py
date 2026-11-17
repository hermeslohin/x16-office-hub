"""X16Link calendar channel (2) end to end over TCP against the fake CalDAV server."""
import asyncio
import sqlite3
import struct

import pytest

from fake_caldav import FakeOC
from test_calsync import TODAY, vevent
from x16hub import calsync as C
from x16hub.link import frame as F
from x16hub.link.client import Client, LinkError
from x16hub.link.server import Limits, LinkServer

KEY = bytes(range(32))
DEV, DEV2 = "x16-test", "x16-other"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


class Hub:
    def __init__(self, tmp):
        self.oc = FakeOC()
        self.keys = {DEV: KEY, DEV2: KEY}
        db = tmp / "hub.db"
        self.srv = LinkServer(
            keys_loader=lambda: self.keys, oc_factory=lambda: self.oc,
            limits=Limits(handshake=2, idle=5, partial=1, resync_quiet=0.05),
            cal_engine_factory=lambda: C.CalSync(self.oc, sqlite3.connect(db), today=lambda: TODAY,
                                                 lock_path=tmp / "lock"),
            cal_db_factory=lambda: sqlite3.connect(db))

    async def __aenter__(self):
        self._s = await self.srv.start("127.0.0.1", 0)
        self.port = self._s.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self._s.close()
        await self._s.wait_closed()

    async def client(self, dev=DEV, maxrx=512):
        return await Client.connect("127.0.0.1", self.port, dev, KEY, maxrx)


def rec(y, m, d, text: bytes, flags=1) -> bytes:
    return C.X16Rec.make(__import__("datetime").date(y, m, d), text, flags).to_bytes()


def all_slots(recs: dict[int, bytes]) -> list[tuple[int, bytes]]:
    return [(s, recs.get(s, bytes(32))) for s in range(128)]


def test_cal_sync_roundtrip_both_ways(tmp_path):
    async def t():
        async with Hub(tmp_path) as h:
            c = await h.client()
            x16 = {}
            r = await c.cal_sync(all_slots(x16), 0)
            assert r["full"] and r["m"] == 128 and r["counter"] >= 0
            # X16 add -> CalDAV
            x16[0] = rec(2026, 11, 20, b"10:00 \xc4ENTIST")
            r = await c.cal_sync(all_slots(x16), r["counter"])
            assert r["created"] == 1
            assert any("Dentist" in v[0] for v in h.oc.objs.values())
            # CalDAV add -> X16
            h.oc.server_put("srv-1", vevent("srv-1", __import__("datetime").datetime(2026, 11, 21, 9, 0,
                                           tzinfo=C.NY), "Lunch"))
            cnt = r["counter"]
            r = await c.cal_sync(all_slots(x16), cnt)
            got = {s: raw for s, raw in r["records"]}
            assert got[1][4:] .startswith(b"09:00 \xccUNCH\0"), got
            st = await c.cal_status()
            assert st["counter"] == r["counter"] and st["server"] == 2
            await c.close()
    run(t())


def test_cal_errors_and_limits(tmp_path):
    async def t():
        async with Hub(tmp_path) as h:
            c = await h.client()
            for op, p in [(F.OP_CAL_PUT, bytes(33)), (F.OP_CAL_SYNC, b"")]:
                with pytest.raises(LinkError) as e:
                    await c.request(F.CH_CAL, op, p)
                assert e.value.code == F.E_BAD_STATE
            with pytest.raises(LinkError) as e:
                await c.request(F.CH_CAL, F.OP_CAL_BEGIN, struct.pack("<IB", 0, 129))
            assert e.value.code == F.E_BAD_ARG
            await c.request(F.CH_CAL, F.OP_CAL_BEGIN, struct.pack("<IB", 0, 2))
            for bad in (bytes([128]) + bytes(32), bytes(32), bytes([1]) + bytes(32) + bytes([1]) + bytes(32),
                        bytes([1]) + bytes(32) + bytes([2]) + bytes(32) + bytes([3]) + bytes(32)):
                with pytest.raises(LinkError) as e:
                    await c.request(F.CH_CAL, F.OP_CAL_PUT, bad)
                assert e.value.code == F.E_BAD_ARG
            await c.request(F.CH_CAL, F.OP_CAL_PUT, bytes([1]) + bytes(32))
            with pytest.raises(LinkError) as e:          # duplicate across frames
                await c.request(F.CH_CAL, F.OP_CAL_PUT, bytes([1]) + bytes(32))
            assert e.value.code == F.E_BAD_ARG
            with pytest.raises(LinkError) as e:          # 1 of 2 staged
                await c.request(F.CH_CAL, F.OP_CAL_SYNC)
            assert e.value.code == F.E_BAD_STATE
            with pytest.raises(LinkError) as e:
                await c.request(F.CH_CAL, F.OP_CAL_GET, bytes([5, 1]))
            assert e.value.code == F.E_BAD_ARG
            with pytest.raises(LinkError) as e:
                await c.request(F.CH_CAL, F.OP_CAL_ACK, bytes([1, 200]))
            assert e.value.code == F.E_BAD_ARG
            with pytest.raises(LinkError) as e:
                await c.request(F.CH_CAL, 0x4F)
            assert e.value.code == F.E_BAD_OP
            assert (await c.noop(b"ok")) == b"ok"         # session survives all of that
            await c.close()
    run(t())


def test_cal_get_respects_small_maxrx(tmp_path):
    async def t():
        async with Hub(tmp_path) as h:
            c = await h.client(maxrx=100)
            await c.request(F.CH_CAL, F.OP_CAL_BEGIN, struct.pack("<IB", 0, 0))
            await c.request(F.CH_CAL, F.OP_CAL_SYNC)
            p = await c.request(F.CH_CAL, F.OP_CAL_GET, bytes([0, 255]))
            assert len(p) <= 100 and p[1] == 2
            await c.close()
    run(t())


def test_cal_bound_to_first_device(tmp_path):
    async def t():
        async with Hub(tmp_path) as h:
            c = await h.client()
            await c.cal_sync([], 0)
            await c.close()
            c2 = await h.client(DEV2)
            with pytest.raises(LinkError) as e:
                await c2.cal_sync([], 0)
            assert e.value.code == F.E_BAD_STATE
            await c2.close()
    run(t())


def test_cal_backend_down_is_backend_error_and_nothing_changes(tmp_path):
    async def t():
        async with Hub(tmp_path) as h:
            c = await h.client()
            r = await c.cal_sync([], 0)
            from x16hub.backend.owncloud import OCError

            def down(*a, **k):
                raise OCError("connection refused")
            h.oc.sync_collection = down
            with pytest.raises(LinkError) as e:
                await c.cal_sync([(0, rec(2026, 11, 20, b"X"))], r["counter"])
            assert e.value.code == F.E_BACKEND
            del h.oc.sync_collection
            assert (await c.cal_status())["counter"] == r["counter"]
            await c.close()
    run(t())


def test_cal_deletes_conflicts_and_ack(tmp_path):
    import datetime as dt

    async def t():
        async with Hub(tmp_path) as h:
            c = await h.client()
            x16 = {}
            r = await c.cal_sync(all_slots(x16), 0)
            x16[0] = rec(2026, 11, 20, b"\xc1LPHA")
            x16[1] = rec(2026, 11, 21, b"\xc2ETA")
            r = await c.cal_sync(all_slots(x16), r["counter"])
            assert r["created"] == 2 and len(h.oc.objs) == 2
            for s, raw in r["records"]:
                x16[s] = raw
            # X16 delete -> CalDAV delete
            del x16[0]
            r = await c.cal_sync(all_slots(x16), r["counter"])
            assert r["deleted"] == 1 and len(h.oc.objs) == 1
            # CalDAV delete -> X16 slot cleared
            uid = next(iter(h.oc.objs))
            h.oc.objs.pop(uid)
            h.oc._bump(uid)
            r = await c.cal_sync(all_slots(x16), r["counter"])
            got = dict(r["records"])
            assert got[1][0] == 0
            x16.pop(1)
            # both edit the same event's text -> conflict, X16 wins, server text reported
            h.oc.server_put("g1", vevent("g1", dt.date(2026, 11, 22), "Gamma"))
            r = await c.cal_sync(all_slots(x16), r["counter"])
            for s, raw in r["records"]:
                x16[s] = raw
            s = next(s for s, raw in x16.items() if raw[0])
            ics = h.oc.ics("g1").replace("SUMMARY:Gamma", "SUMMARY:Gamma server")
            h.oc.server_put("g1", ics)
            x16[s] = bytes([3]) + x16[s][1:4] + b"\xc7AMMA X16".ljust(28, b"\0")
            r = await c.cal_sync(all_slots(x16), r["counter"])
            assert r["conflicts"] == 1
            conf = await c.cal_conflicts()
            assert conf == [{"slot": s, "kind": 0, "field": 3, "kept": 0, "value": b"\xc7AMMA SERVER"}], conf
            assert dict(r["records"])[s][0] & 4               # conflict flag on the X16 record
            assert await c.cal_ack([s]) == 1
            assert (await c.cal_status())["conflicts"] == 0
            await c.close()
    run(t())
