"""X16Link v1 asyncio TCP server (`python -m x16hub serve`). See PROTOCOL.md.

Hardening:
  * LAN-only: bind address from config, peers outside the allow-list are dropped
    before a byte is read; connection caps (total / per IP).
  * Every read has a timeout (handshake deadline, idle timeout, partial-frame
    timeout); writes time out too (a client that stops reading is dropped).
  * Frames: hard payload cap (64 before auth, <=1024 after), CRC-16 checked;
    garbage triggers an idle-resync and an ERR, 3 bad frames in a row close the
    session, any bad frame before auth closes it.
  * Auth: HMAC-SHA256 challenge-response with a fresh 16-byte CSPRNG nonce,
    constant-time compare, same answer for unknown device and wrong key,
    per-IP failure lockout.
"""
from __future__ import annotations

import asyncio
import collections
import hmac
import ipaddress
import logging
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime

from . import frame as F
from . import keys as K
from .cal import CalChannel, device_binder
from .docs import TZ, DocError, DocsChannel, PendingUploads
from .frame import Frame, FrameError, Reader, pstr

log = logging.getLogger("x16hub.link")


@dataclass
class Limits:
    handshake: float = 15.0
    idle: float = 300.0
    partial: float = 10.0
    write: float = 30.0
    resync_quiet: float = 0.3
    resync_max_time: float = 5.0
    resync_max_bytes: int = 1 << 16
    max_conns: int = 4
    max_conns_per_ip: int = 2
    auth_fail_max: int = 5
    auth_fail_window: float = 600.0
    auth_block: float = 600.0
    max_bad_frames: int = 3
    hello: float = 5.0          # first frame (HELLO) must arrive within this
    evict_after: float = 3.0    # when full, a newcomer evicts the oldest unauthenticated conn older than this
    hub_maxrx: int = F.ABS_MAX_PAYLOAD


class Hangup(Exception):
    """Peer closed / hung up (EOF or Zimodem '+++')."""


# channel number -> factory(session) -> object with `async handle(op, payload) -> bytes`
CHANNELS: dict[int, callable] = {}


def register_channel(chan: int, factory) -> None:
    if chan in (F.CH_CTRL,) or not 0 < chan < 256:
        raise ValueError("bad channel number")
    CHANNELS[chan] = factory


def _docs_factory(session: "Session"):
    return DocsChannel(session.server.oc(), session.device, session.server.pending, session.peer_maxrx)


register_channel(F.CH_DOCS, _docs_factory)


def _cal_factory(session: "Session"):
    return CalChannel(session.server.cal_engine(), session.device, session.peer_maxrx,
                      session.server.cal_bind)


register_channel(F.CH_CAL, _cal_factory)


class LinkServer:
    def __init__(self, *, keys_loader=K.load_keys, oc_factory=None, limits: Limits | None = None,
                 allow: list | None = None, hubname: str = "x16hub", calendar: str = "x16",
                 cal_engine_factory=None, cal_db_factory=None):
        self.keys_loader = keys_loader
        self._oc_factory = oc_factory
        self.limits = limits or Limits()
        self.allow = allow if allow is not None else K.parse_allow(K.DEFAULTS["allow"])
        self.hubname = hubname
        self.calendar = calendar
        self._cal_engine_factory = cal_engine_factory
        if cal_db_factory is None:
            from ..store import connect as cal_db_factory
        self.cal_bind = device_binder(cal_db_factory)
        self.pending = PendingUploads()
        self.sessions: set = set()
        self.active: collections.Counter = collections.Counter()
        self.fails: dict[str, collections.deque] = {}
        self.blocked: dict[str, float] = {}
        self.server: asyncio.base_events.Server | None = None

    def oc(self):
        if self._oc_factory is None:
            from ..backend import owncloud as oc
            cfg = oc.load_config()
            self._oc_factory = lambda: oc.OwnCloud(cfg)
        return self._oc_factory()

    def cal_engine(self):
        """-> zero-arg factory for a fresh CalSync (called in a worker thread per request)."""
        if self._cal_engine_factory is None:
            from .. import calsync
            self.oc()                        # loads/validates the ownCloud config now
            ocf, cal = self._oc_factory, self.calendar
            self._cal_engine_factory = lambda: calsync.CalSync(ocf(), cal_name=cal)
        return self._cal_engine_factory

    async def start(self, host: str, port: int):
        self.server = await asyncio.start_server(self._accept, host, port, limit=4096,
                                                 reuse_address=True)
        return self.server

    # ---- admission control
    @staticmethod
    def peer_key(ip: str) -> str:
        """Counting/blocking unit: an IPv4 address, or the /64 of an IPv6 one (a host can pick any
        address in its /64, so per-address limits would be meaningless)."""
        try:
            a = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return ip
        if getattr(a, "ipv4_mapped", None):
            return str(a.ipv4_mapped)
        if a.version == 6:
            return str(ipaddress.ip_network(f"{a}/64", strict=False))
        return str(a)

    def _evict_preauth(self) -> bool:
        now = time.monotonic()
        # only connections that never sent a valid HELLO: a real X16 sends it right after CONNECT,
        # so one that is mid-handshake (HELLO done, computing its HMAC) is never evicted
        cands = [s for s in self.sessions if not s.authed and not s.hello_ok and now - s.t0 >= self.limits.evict_after]
        if not cands:
            return False
        s = min(cands, key=lambda s: s.t0)
        log.warning("evicting unauthenticated connection from %s to make room", s.ip)
        s.evicted = True
        self.auth_failed(s.ip)                       # squatting a slot costs, so churning gets blocked
        s.w.close()
        return True

    def _allowed(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return any(a in n for n in self.allow)

    @staticmethod
    def addr_key(ip: str) -> str:
        """Exact address (IPv4-mapped -> IPv4, scope id dropped)."""
        try:
            a = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return ip
        return str(a.ipv4_mapped) if getattr(a, "ipv4_mapped", None) else str(a)

    def _keys(self, ip: str) -> list[tuple[str, int]]:
        """(key, failure limit): the address itself, plus its IPv6 /64 with a 4x higher limit, so
        rotating addresses inside a /64 doesn't escape the lockout but one host on a shared /64
        (link-local, a ULA subnet) needs 4x the failures to affect the others."""
        a, n = self.addr_key(ip), self.limits.auth_fail_max
        p = self.peer_key(ip)
        return [(a, n)] if p == a else [(a, n), (p, 4 * n)]

    def is_blocked(self, ip: str) -> bool:
        now = time.monotonic()
        for k, _ in self._keys(ip):
            until = self.blocked.get(k)
            if until and until > now:
                return True
            self.blocked.pop(k, None)
        return False

    def auth_failed(self, ip: str) -> None:
        now = time.monotonic()
        for k, limit in self._keys(ip):
            dq = self.fails.setdefault(k, collections.deque())
            dq.append(now)
            while dq and now - dq[0] > self.limits.auth_fail_window:
                dq.popleft()
            if len(dq) >= limit:
                self.blocked[k] = now + self.limits.auth_block
                dq.clear()
                log.warning("blocking %s for %ds after repeated auth failures", k, self.limits.auth_block)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = str(peer[0])
        pk = self.addr_key(ip)
        reason = None
        if not self._allowed(ip):
            reason = "not in allow-list"
        elif self.is_blocked(ip):
            reason = "blocked (auth failures)"
        elif self.active[pk] >= self.limits.max_conns_per_ip:
            reason = "too many connections from this address"
        elif sum(self.active.values()) >= self.limits.max_conns and not self._evict_preauth():
            reason = "too many connections"
        if reason:
            log.warning("refused %s: %s", ip, reason)
            writer.close()
            return
        self.active[pk] += 1
        s = Session(self, reader, writer, ip)
        self.sessions.add(s)
        try:
            await s.run()
        except Exception:  # noqa: BLE001 - a session bug must never kill the server
            log.exception("session %s crashed", ip)
        finally:
            self.sessions.discard(s)
            self.active[pk] -= 1
            if self.active[pk] <= 0:
                del self.active[pk]
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                pass


class Session:
    def __init__(self, server: LinkServer, reader, writer, ip: str):
        self.server, self.r, self.w, self.ip = server, reader, writer, ip
        self.L = server.limits
        self.authed = False
        self.device = ""
        self.peer_maxrx = 64
        self.channels: dict[int, object] = {}
        self.last_req: tuple | None = None
        self.last_reply: bytes = b""
        self.bad = 0
        self.t0 = time.monotonic()
        self.failed_counted = False
        self.hello_ok = False
        self.evicted = False

    # ---- I/O
    async def _read(self, n: int, timeout: float) -> bytes:
        try:
            return await asyncio.wait_for(self.r.readexactly(n), timeout)
        except asyncio.IncompleteReadError:
            raise Hangup() from None
        except (ConnectionError, OSError):
            raise Hangup() from None

    async def send(self, chan: int, seq: int, op: int, payload: bytes = b"") -> bytes:
        data = F.encode(chan, seq, op, payload)
        self.w.write(data)
        try:
            await asyncio.wait_for(self.w.drain(), self.L.write)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            raise Hangup() from None
        return data

    async def send_err(self, seq: int, code: int, msg: str, extra: bytes = b"", chan: int = F.CH_CTRL) -> bytes:
        return await self.send(chan, seq, F.OP_ERR, F.err_payload(code, msg, extra))

    async def resync(self) -> None:
        """Skip input until the line has been quiet for resync_quiet seconds (bounded)."""
        deadline = time.monotonic() + self.L.resync_max_time
        skipped = 0
        while time.monotonic() < deadline and skipped < self.L.resync_max_bytes:
            try:
                b = await asyncio.wait_for(self.r.read(512), self.L.resync_quiet)
            except asyncio.TimeoutError:
                return
            if not b:
                raise Hangup()
            skipped += len(b)
        raise Hangup()  # never went quiet: hostile or broken, drop it

    async def read_frame(self, max_payload: int, first_timeout: float) -> Frame:
        b0 = (await self._read(1, first_timeout))[0]
        try:
            b1 = (await self._read(1, self.L.partial))[0]
        except asyncio.TimeoutError:
            if b0 == 0x2B:
                raise Hangup() from None    # lone '+' then silence: Zimodem escape
            raise
        if b0 == 0x2B and b1 == 0x2B:
            # Zimodem forwards the guarded '+++' hangup escape; a real frame can't start with 2B 2B.
            try:
                b2 = (await self._read(1, 2.0))[0]
            except asyncio.TimeoutError:
                raise Hangup() from None
            if b2 != 0x2B:
                raise FrameError(F.E_BAD_FRAME, "garbage")
            raise Hangup()
        n = b0 | (b1 << 8)
        if n > max_payload:
            raise FrameError(F.E_TOO_BIG, "frame too big")
        rest = await self._read(3 + n + 2, self.L.partial)
        fr, _ = F.decode(bytes([b0, b1]) + rest, max_payload)
        return fr

    # ---- main loop
    async def run(self) -> None:
        log.info("connection from %s", self.ip)
        try:
            try:
                await asyncio.wait_for(self.handshake(), self.L.handshake)
            except asyncio.TimeoutError:
                self.server.auth_failed(self.ip)     # holding a slot without authenticating costs
                await self.send_err(0xFF, F.E_TIMEOUT, "handshake timeout")
                return
            except Hangup:
                if not self.evicted:
                    self.server.auth_failed(self.ip)
                raise
            if not self.authed:
                if not self.failed_counted:
                    self.server.auth_failed(self.ip)
                return
            await self.serve()
        except Hangup:
            pass
        except asyncio.TimeoutError:
            try:
                await self.send_err(0xFF, F.E_TIMEOUT, "timeout")
            except Hangup:
                pass
        finally:
            log.info("closed %s (device %s)", self.ip, self.device or "-")

    async def handshake(self) -> None:
        try:
            fr = await self.read_frame(F.PREAUTH_MAX_PAYLOAD, self.L.hello)
        except FrameError as e:
            await self.send_err(0xFF, e.code, str(e))
            return
        if fr.chan != F.CH_CTRL or fr.op != F.OP_HELLO:
            await self.send_err(fr.seq, F.E_BAD_STATE, "HELLO expected")
            return
        try:
            r = Reader(fr.payload)
            if r.raw(4) != F.MAGIC:
                raise FrameError(F.E_BAD_FRAME, "bad magic")
            ver, maxrx, devid, cnonce = r.u8(), r.u16(), r.str_(), r.raw(16)
            r.end()
        except FrameError as e:
            await self.send_err(fr.seq, e.code, str(e))
            return
        if ver != F.VERSION:
            await self.send_err(fr.seq, F.E_BAD_ARG, "unsupported protocol version")
            return
        if maxrx < 64:
            await self.send_err(fr.seq, F.E_BAD_ARG, "maxrx must be >= 64")
            return
        dev = devid.decode("ascii", "replace")
        if not K.DEVID_RE.fullmatch(dev):
            await self.send_err(fr.seq, F.E_AUTH_FAIL, "authentication failed")
            self.server.auth_failed(self.ip)
            self.failed_counted = True
            return
        self.peer_maxrx = min(maxrx, F.ABS_MAX_PAYLOAD)
        self.hello_ok = True
        snonce = secrets.token_bytes(16)
        await self.send(F.CH_CTRL, fr.seq, F.OP_HELLO | F.REPLY,
                        bytes([F.VERSION]) + struct.pack("<H", self.L.hub_maxrx) + snonce + pstr(self.server.hubname))
        try:
            fr2 = await self.read_frame(F.PREAUTH_MAX_PAYLOAD, self.L.handshake)
        except FrameError as e:
            await self.send_err(0xFF, e.code, str(e))
            return
        try:
            keys = self.server.keys_loader()
        except Exception as e:  # noqa: BLE001
            log.error("cannot load link keys: %s", e)
            await self.send_err(fr2.seq, F.E_BACKEND, "hub key store unavailable")
            return
        key = keys.get(dev)
        t = K.transcript(devid, cnonce, snonce, F.VERSION)
        expect = K.client_tag(key if key else secrets.token_bytes(32), t)
        ok = (fr2.chan == F.CH_CTRL and fr2.op == F.OP_AUTH and len(fr2.payload) == 32
              and hmac.compare_digest(fr2.payload, expect) and key is not None)
        if not ok:
            log.warning("auth failed from %s (device %r)", self.ip, dev)
            self.server.auth_failed(self.ip)
            self.failed_counted = True
            await self.send_err(fr2.seq, F.E_AUTH_FAIL, "authentication failed")
            return
        self.authed, self.device = True, dev
        reply = await self.send(F.CH_CTRL, fr2.seq, F.OP_AUTH | F.REPLY, K.server_tag(key, t))
        # A lossy serial link can garble this reply; the X16 then resends the identical AUTH frame
        # (same seq). Let serve() replay the same bytes instead of answering "already authenticated".
        self.last_req = (fr2.chan, fr2.seq, fr2.op, fr2.payload)
        self.last_reply = reply
        log.info("device %s authenticated from %s", dev, self.ip)

    async def serve(self) -> None:
        maxp = min(self.L.hub_maxrx, F.ABS_MAX_PAYLOAD)
        while True:
            try:
                fr = await self.read_frame(maxp, self.L.idle)
            except FrameError as e:
                self.bad += 1
                if self.bad >= self.L.max_bad_frames:
                    await self.send_err(0xFF, e.code, "too many bad frames; closing")
                    return
                await self.resync()
                await self.send_err(0xFF, e.code, str(e))
                continue
            self.bad = 0
            key = (fr.chan, fr.seq, fr.op, fr.payload)
            if key == self.last_req:
                self.w.write(self.last_reply)                 # retransmitted request: replay
                await asyncio.wait_for(self.w.drain(), self.L.write)
                continue
            reply = await self.dispatch(fr)
            self.last_req, self.last_reply = key, reply
            if fr.chan == F.CH_CTRL and fr.op == F.OP_BYE:
                return

    async def dispatch(self, fr: Frame) -> bytes:
        try:
            if fr.chan == F.CH_CTRL:
                payload = self.control(fr)
            else:
                ch = self.channels.get(fr.chan)
                if ch is None:
                    factory = CHANNELS.get(fr.chan)
                    if factory is None:
                        raise DocError(F.E_BAD_OP, "unknown channel")
                    try:
                        ch = self.channels[fr.chan] = factory(self)
                    except Exception as e:  # noqa: BLE001
                        log.error("channel %d unavailable: %s", fr.chan, e)
                        raise DocError(F.E_BACKEND, "backend unavailable") from None
                payload = await ch.handle(fr.op, fr.payload)
            return await self.send(fr.chan, fr.seq, fr.op | F.REPLY, payload)
        except DocError as e:
            return await self.send_err(fr.seq, e.code, str(e), e.extra, chan=fr.chan)
        except FrameError as e:
            return await self.send_err(fr.seq, e.code, str(e), chan=fr.chan)
        except UnicodeError:
            return await self.send_err(fr.seq, F.E_BAD_FRAME, "bad text encoding", chan=fr.chan)
        except (Hangup, asyncio.TimeoutError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - a handler bug or odd backend data: answer, keep the session
            log.exception("op %s on channel %d failed", F.OP_NAMES.get(fr.op, hex(fr.op)), fr.chan)
            return await self.send_err(fr.seq, F.E_BACKEND, "internal hub error", chan=fr.chan)

    def control(self, fr: Frame) -> bytes:
        if fr.op == F.OP_NOOP:
            if len(fr.payload) > 64:
                raise DocError(F.E_BAD_ARG, "NOOP payload too long")
            return fr.payload
        if fr.op == F.OP_TIME:
            Reader(fr.payload).end()
            return time_payload()
        if fr.op == F.OP_BYE:
            return b""
        if fr.op in (F.OP_HELLO, F.OP_AUTH):
            raise DocError(F.E_BAD_STATE, "already authenticated")
        raise DocError(F.E_BAD_OP, "unknown control op")


def time_payload(now: datetime | None = None) -> bytes:
    d = (now or datetime.now(TZ)).astimezone(TZ)
    off = int(d.utcoffset().total_seconds() // 60)
    dst = 1 if d.dst() and d.dst().total_seconds() else 0
    wday = (d.weekday() + 1) % 7      # Python Monday=0 -> Sunday=0
    return (struct.pack("<HBBBBBBhB", d.year, d.month, d.day, d.hour, d.minute, d.second, wday, off, dst)
            + pstr(d.tzname() or ""))


async def serve_forever(host: str, port: int, **kw) -> None:
    srv = LinkServer(**kw)
    s = await srv.start(host, port)
    addrs = ", ".join(str(sock.getsockname()) for sock in s.sockets)
    log.warning("x16link listening on %s", addrs)
    async with s:
        await s.serve_forever()
