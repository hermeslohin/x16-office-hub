"""X16Link calendar channel (2): wire glue for x16hub.calsync (PROTOCOL.md section 3.5).

The X16 stages its slots with CAL_BEGIN + CAL_PUT (chunked, a frame is too small for all 128),
runs CAL_SYNC, then fetches the changed slots with CAL_GET. The engine (CalSync) is blocking and
owns a SQLite connection, so every engine call runs in a worker thread with its own connection.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct

from . import frame as F
from .docs import DocError
from .frame import Reader

log = logging.getLogger(__name__)

OP_CAL_BEGIN, OP_CAL_PUT, OP_CAL_SYNC, OP_CAL_GET = 0x30, 0x31, 0x32, 0x33
OP_CAL_CONFLICTS, OP_CAL_ACK, OP_CAL_STATUS = 0x34, 0x35, 0x36

NSLOTS, RECSZ, TXTMAX = 128, 32, 27
FIELDS = {"event": 0, "date": 1, "time": 2, "text": 3}
KEPT = {"x16": 0, "server": 1}
KINDS = {"both-edited": 0, "x16-delete-vs-server-edit": 1, "x16-edit-vs-server-delete": 2}
CONF_SZ = 4 + TXTMAX


def _u8(n: int) -> int:
    return max(0, min(255, int(n)))


class CalChannel:
    """Per-session calendar state. `engine_factory()` -> a fresh CalSync (called in the worker thread)."""

    def __init__(self, engine_factory, device: str, peer_maxrx: int, bind_device):
        self.engine_factory, self.device, self.maxrx = engine_factory, device, peer_maxrx
        self.bind_device = bind_device          # (device) -> None, raises DocError if bound to another one
        self.staged: dict[int, bytes] | None = None
        self.expect = 0
        self.since = 0
        self.result: list[tuple[int, bytes]] = []
        self.conf: list[tuple[int, int, int, int, bytes]] = []

    # ------------------------------------------------------------ engine plumbing
    async def _engine(self, fn):
        from ..backend.owncloud import OCError
        from ..store import HubError

        def run():
            self.bind_device(self.device)
            cs = self.engine_factory()
            try:
                return fn(cs)
            finally:
                try:
                    cs.db.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            return await asyncio.to_thread(run)
        except DocError:
            raise
        except calsync_MassDelete() as e:
            log.warning("calendar sync dev=%s: %s", self.device, e)
            raise DocError(F.E_BAD_STATE, f"refused: would delete {e.n} events") from None
        except OCError as e:
            log.warning("calendar sync: ownCloud error: %s", e)
            raise DocError(F.E_BACKEND, "ownCloud unreachable, nothing changed") from None
        except HubError as e:
            log.warning("calendar sync: %s", e)
            raise DocError(F.E_BACKEND, "calendar sync failed, nothing changed") from None
        except (sqlite3.Error, OSError) as e:
            log.error("calendar sync: local error: %s", e)
            raise DocError(F.E_BACKEND, "hub storage error") from None

    async def handle(self, op: int, payload: bytes) -> bytes:
        r = Reader(payload)
        if op == OP_CAL_BEGIN:
            return self.begin(r)
        if op == OP_CAL_PUT:
            return self.put(r)
        if op == OP_CAL_SYNC:
            r.end()
            return await self.sync()
        if op == OP_CAL_GET:
            return self.get(r)
        if op == OP_CAL_CONFLICTS:
            return self.conflicts(r)
        if op == OP_CAL_ACK:
            return await self.ack(r)
        if op == OP_CAL_STATUS:
            r.end()
            return await self.status()
        raise DocError(F.E_BAD_OP, "unknown calendar op")

    # ------------------------------------------------------------ ops
    def begin(self, r: Reader) -> bytes:
        since, n = r.u32(), r.u8()
        r.end()
        if n > NSLOTS:
            raise DocError(F.E_BAD_ARG, "too many records")
        self.staged, self.expect, self.since = {}, n, since
        self.result, self.conf = [], []
        return b""

    def put(self, r: Reader) -> bytes:
        if self.staged is None:
            raise DocError(F.E_BAD_STATE, "CAL_BEGIN first")
        rest = r.rest()
        if not rest or len(rest) % (RECSZ + 1):
            raise DocError(F.E_BAD_ARG, "records must be slot + 32 bytes")
        batch = {}
        for i in range(0, len(rest), RECSZ + 1):
            slot = rest[i]
            if slot >= NSLOTS or slot in self.staged or slot in batch:
                raise DocError(F.E_BAD_ARG, "bad or duplicate slot")
            batch[slot] = bytes(rest[i + 1:i + 1 + RECSZ])
        if len(self.staged) + len(batch) > self.expect:
            raise DocError(F.E_BAD_ARG, "more records than announced")
        self.staged.update(batch)
        return bytes([len(self.staged)])

    async def sync(self) -> bytes:
        if self.staged is None or len(self.staged) != self.expect:
            raise DocError(F.E_BAD_STATE, "not all records sent")
        recs, since = sorted(self.staged.items()), self.since
        self.staged = None                       # one CAL_SYNC per CAL_BEGIN

        def go(cs):
            push, pull = cs.sync(recs, since)
            return push, pull, self._enc_conflicts(cs, pull.conflicts)

        push, pull, conf = await self._engine(go)
        self.result, self.conf = list(pull.records), conf
        log.info("calendar sync dev=%s since=%d -> counter=%d sent=%d created=%d updated=%d deleted=%d "
                 "conflicts=%d unplaced=%d", self.device, since, pull.counter, len(self.result), push.created,
                 push.updated, push.deleted, len(conf), pull.unplaced)
        return (struct.pack("<IBB", pull.counter, 1 if pull.full else 0, len(self.result))
                + bytes([_u8(push.created), _u8(push.updated), _u8(push.deleted), _u8(len(conf)),
                         _u8(pull.unplaced), _u8(push.unsyncable)]))

    def get(self, r: Reader) -> bytes:
        idx, mx = r.u8(), r.u8()
        r.end()
        if idx > len(self.result):
            raise DocError(F.E_BAD_ARG, "index past end")
        fit = max(1, (self.maxrx - 2) // (RECSZ + 1))
        part = self.result[idx:idx + min(mx, fit)]
        out = bytearray([idx, len(part)])
        for slot, raw in part:
            out.append(slot)
            out += raw
        return bytes(out)

    def _enc_conflicts(self, cs, rows) -> list[tuple[int, int, int, int, bytes]]:
        out = []
        for c in rows:
            slot = c.get("slot")
            if slot is None or not 0 <= int(slot) < NSLOTS:
                slot = 255                       # event not on the X16 (yet)
            val = cs.cs.encode(str(c.get("server_value") or ""))[:TXTMAX]
            out.append((int(slot), KINDS.get(c.get("kind"), 0), FIELDS.get(c.get("field"), 3), KEPT.get(c.get("kept"), 1),
                        val))
        return out

    def conflicts(self, r: Reader) -> bytes:
        start = r.u8()
        r.end()
        fit = max(1, (self.maxrx - 3) // CONF_SZ)
        part = self.conf[start:start + fit]
        out = bytearray([_u8(len(self.conf)), start, len(part)])
        for slot, kind, fld, kept, val in part:
            out += bytes([slot, kind, fld, kept]) + val.ljust(TXTMAX, b"\0")
        return bytes(out)

    async def ack(self, r: Reader) -> bytes:
        k = r.u8()
        slots = [r.u8() for _ in range(k)]
        r.end()
        if any(s >= NSLOTS for s in slots):
            raise DocError(F.E_BAD_ARG, "bad slot")
        n = await self._engine(lambda cs: cs.ack_conflicts(slots if k else None))
        self.conf = [c for c in self.conf if k and c[0] not in slots]
        return bytes([_u8(n)])

    async def status(self) -> bytes:
        st = await self._engine(lambda cs: cs.status())
        return struct.pack("<IBHBB", st["counter"], _u8(st["x16_used"]), min(65535, st["server_objects"]),
                           _u8(st["unplaced"]), _u8(st["open_conflicts"]))


def calsync_MassDelete():
    from ..calsync import MassDelete
    return MassDelete


def device_binder(db_factory):
    """One X16 per calendar: the hub's slot map and counter describe one CALENDAR.DAT. The first
    device that syncs owns it; others get BAD_STATE until `x16hub.calsync reset`."""
    def bind(device: str) -> None:
        db = db_factory()
        try:
            db.execute("CREATE TABLE IF NOT EXISTS cal_meta (k TEXT PRIMARY KEY, v TEXT)")
            db.execute("INSERT OR IGNORE INTO cal_meta(k,v) VALUES('x16_device',?)", (device,))
            db.commit()
            row = db.execute("SELECT v FROM cal_meta WHERE k='x16_device'").fetchone()
        finally:
            db.close()
        if row and row[0] != device:
            raise DocError(F.E_BAD_STATE, "calendar belongs to another X16 (hub: calsync reset)")
    return bind
