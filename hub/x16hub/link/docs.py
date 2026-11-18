"""Docs channel (1): LIST / OPEN / READ / SAVE / WRITE / COMMIT on ownCloud Documents/, plus the folder
ops DIR / MKDIR / DELETE / MOVE / COPY (X16 file manager, Save As folder browser)."""
from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from . import frame as F
from .frame import Reader, pstr
from . import docxconv, sheetconv
from .ooxml import ConvError
from .rtfconv import WP_CHARSET, decode_utf8, rtf_to_text, text_to_rtf, to_wire_text

log = logging.getLogger("x16hub.link.docs")

DOCS_ROOT = "Documents"
MAX_FILE = 1 << 20             # 1 MiB, either direction
MAX_NAME = 63                  # bytes on the wire (ISO-8859-15)
MAX_LIST = 1000
MAX_DEPTH = 8
MAX_FOLDERS = 200
PENDING_TTL = 15 * 60
MAX_PENDING_PER_DEV = 2
TZ = ZoneInfo("America/New_York")
_BAD_CHARS = set('\\:*?"<>|')

T_OTHER, T_RTF, T_TXT, T_DOCX, T_XLSX, T_CSV = 0, 1, 2, 3, 4, 5
T_DIR = 6                       # DIR entries only: a folder
_EXT = {".rtf": T_RTF, ".txt": T_TXT, ".docx": T_DOCX, ".xlsx": T_XLSX, ".csv": T_CSV}
# conv 0 = native bytes, 1 = RTF on the X16 side, 2 = X16 sheet (simple XLSX) on the X16 side,
# 3 = raw: byte-for-byte, any file type (file manager copies)
CONV_RAW = 3
CONV_OK = {T_RTF: (0, 1), T_TXT: (0, 1), T_DOCX: (0, 1), T_XLSX: (0, 2), T_CSV: (0, 2)}
FL_UNSUPPORTED, FL_LOSSY, FL_TOO_LARGE = 1, 2, 4

# One format conversion at a time across all sessions: each is bounded in memory
# (ooxml caps), and serializing keeps the sum under the unit's MemoryMax.
_CONV_LOCK = asyncio.Semaphore(1)


async def _convert(fn, *a):
    async with _CONV_LOCK:
        return await asyncio.to_thread(fn, *a)


class DocError(Exception):
    def __init__(self, code: int, msg: str, extra: bytes = b""):
        super().__init__(msg)
        self.code, self.extra = code, extra


def file_type(name: str) -> int:
    low = name.lower()
    dot = low.rfind(".")
    return _EXT.get(low[dot:], T_OTHER) if dot >= 0 else T_OTHER


def check_conv(t: int, conv: int) -> None:
    if conv not in (0, 1, 2, CONV_RAW):
        raise DocError(F.E_BAD_ARG, "bad conv")
    if conv == CONV_RAW:
        return
    if t == T_OTHER:
        raise DocError(F.E_UNSUPPORTED, "file type not supported")
    if conv not in CONV_OK[t]:
        raise DocError(F.E_UNSUPPORTED, "conversion not available for this file type")


def check_name(name: str, *, for_save: bool = False) -> str:
    """Validate a Documents-relative path. Returns the normalised name or raises DocError."""
    if not name or len(name.encode(WP_CHARSET, "replace")) > MAX_NAME:
        raise DocError(F.E_BAD_NAME, "name empty or too long")
    segs = name.split("/")
    for s in segs:
        if s in ("", ".", ".."):
            raise DocError(F.E_BAD_NAME, "bad path segment")
        if s[0] in ". " or s[-1] == " ":
            raise DocError(F.E_BAD_NAME, "name may not start with '.' or space")
        for ch in s:
            o = ord(ch)
            if o < 0x20 or 0x7F <= o <= 0x9F or ch in _BAD_CHARS:
                raise DocError(F.E_BAD_NAME, "name has a forbidden character")
    if for_save and file_type(name) == T_OTHER:
        raise DocError(F.E_BAD_NAME, "name must end in .rtf .txt .docx .xlsx or .csv")
    return "/".join(segs)


def wire_name(b: bytes, *, for_save: bool = False) -> str:
    return check_name(b.decode(WP_CHARSET), for_save=for_save)  # every byte decodes; C1 rejected in check


def wire_dir(b: bytes) -> str:
    """A folder path for DIR: empty = Documents/ itself, else the usual name rules."""
    return wire_name(b) if b else ""


def _unix(last_modified: str | None) -> int:
    try:
        return int(parsedate_to_datetime(last_modified).timestamp()) if last_modified else 0
    except (TypeError, ValueError):
        return 0


def _local5(ts: int) -> bytes:
    if ts <= 0:
        return bytes(5)
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    return bytes([max(0, min(255, d.year - 2000)), d.month, d.day, d.hour, d.minute])


@dataclass
class Entry:
    name: str
    size: int
    mtime: int
    etag: str | None
    is_dir: bool = False
    raw_ok: bool = False        # DIR: any type can be moved raw (conv 3); "unsupported" = can't transfer

    def encode(self) -> bytes:
        t = T_DIR if self.is_dir else file_type(self.name)
        wn, lossy = to_wire_text(self.name)
        flags = 0
        if lossy:
            flags |= FL_LOSSY
        if self.size > MAX_FILE:
            flags |= FL_TOO_LARGE
        try:
            check_name(self.name)
            bad = False
        except DocError:
            bad = True
        if (t == T_OTHER and not self.raw_ok) or bad or lossy or len(wn) > MAX_NAME or self.size > MAX_FILE:
            flags |= FL_UNSUPPORTED
        return (bytes([t, flags]) + struct.pack("<II", min(self.size, 0xFFFFFFFF), self.mtime & 0xFFFFFFFF)
                + _local5(self.mtime) + pstr(wn[:255]))


@dataclass
class Upload:
    size: int
    crc: int
    buf: bytearray = field(default_factory=bytearray)
    created: float = field(default_factory=time.monotonic)


class PendingUploads:
    """Server-wide (shared by sessions) so a dropped connection can resume."""

    def __init__(self):
        self.by_dev: dict[str, dict[tuple[int, int], Upload]] = {}

    def get(self, dev: str, size: int, crc: int) -> Upload:
        now = time.monotonic()
        ups = self.by_dev.setdefault(dev, {})
        for k in [k for k, u in ups.items() if now - u.created > PENDING_TTL]:
            del ups[k]
        u = ups.get((size, crc))
        if u is None:
            while len(ups) >= MAX_PENDING_PER_DEV:
                del ups[min(ups, key=lambda k: ups[k].created)]
            u = ups[(size, crc)] = Upload(size, crc)
        return u

    def drop(self, dev: str, size: int, crc: int) -> None:
        self.by_dev.get(dev, {}).pop((size, crc), None)


class DocsChannel:
    """Per-session docs state. `oc` is an OwnCloud-like client (blocking; run in a thread)."""

    def __init__(self, oc, device: str, pending: PendingUploads, peer_maxrx: int):
        self.oc, self.device, self.pending, self.maxrx = oc, device, pending, peer_maxrx
        self.snapshot: list[Entry] = []
        self.dir_snapshot: list[Entry] = []
        self.open_data: bytes | None = None
        self.save: dict | None = None

    async def _call(self, fn, *a, **kw):
        from ..backend.owncloud import NotFound, OCError, PreconditionFailed, TooLarge
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except PreconditionFailed:
            raise
        except TooLarge:
            raise DocError(F.E_TOO_LARGE, "file is larger than 1 MiB") from None
        except NotFound:
            raise DocError(F.E_NOT_FOUND, "file not found") from None
        except OCError as e:
            log.warning("ownCloud error: %s", e)
            if e.status == 409:
                raise DocError(F.E_BAD_NAME, "folder does not exist") from None
            raise DocError(F.E_BACKEND, f"ownCloud error{f' (HTTP {e.status})' if e.status else ''}") from None

    async def handle(self, op: int, payload: bytes) -> bytes:
        r = Reader(payload)
        if op == F.OP_LIST:
            return await self.list(r)
        if op == F.OP_OPEN:
            return await self.open(r)
        if op == F.OP_READ:
            return self.read(r)
        if op == F.OP_SAVE:
            return self.save_begin(r)
        if op == F.OP_WRITE:
            return self.write(r)
        if op == F.OP_COMMIT:
            r.end()
            return await self.commit()
        if op == F.OP_DIR:
            return await self.dir(r)
        if op == F.OP_MKDIR:
            return await self.mkdir(r)
        if op == F.OP_DELETE:
            return await self.delete(r)
        if op in (F.OP_MOVE, F.OP_COPY):
            return await self.transfer(op, r)
        raise DocError(F.E_BAD_OP, "unknown docs op")

    # ---- LIST
    def _walk(self) -> list[Entry]:
        self.oc.ensure_folder(DOCS_ROOT)
        out: list[Entry] = []
        todo = [(DOCS_ROOT, 0)]
        folders = 0
        prefix = DOCS_ROOT + "/"
        while todo and len(out) < MAX_LIST:
            path, depth = todo.pop()
            folders += 1
            if folders > MAX_FOLDERS:
                break
            for it in self.oc.list(path):
                p = it["path"].rstrip("/")
                if p == path or not p.startswith(prefix):
                    continue
                if it["is_dir"]:
                    if depth + 1 < MAX_DEPTH:
                        todo.append((p, depth + 1))
                    continue
                out.append(Entry(p[len(prefix):], it["size"], _unix(it["last_modified"]), it["etag"]))
                if len(out) >= MAX_LIST:
                    break
        out.sort(key=lambda e: e.name.casefold())
        return out

    async def list(self, r: Reader) -> bytes:
        start, mx = r.u16(), r.u8()
        r.end()
        if start == 0:
            self.snapshot = await self._call(self._walk)
        return self._page(self.snapshot, start, mx)

    def _page(self, snap: list[Entry], start: int, mx: int) -> bytes:
        total = len(snap)
        if start > total:
            raise DocError(F.E_BAD_ARG, "start beyond end of list")
        body = b""
        n = 0
        room = self.maxrx - 5
        for e in snap[start:start + max(0, mx)]:
            enc = e.encode()
            if len(body) + len(enc) > room:
                break
            body += enc
            n += 1
        return struct.pack("<HHB", total, start, n) + body

    # ---- DIR / MKDIR / DELETE / MOVE / COPY
    def _dir(self, rel: str) -> list[Entry]:
        self.oc.ensure_folder(DOCS_ROOT)
        path = f"{DOCS_ROOT}/{rel}" if rel else DOCS_ROOT
        st = self.oc.stat(path)
        if st is None or not st["is_dir"]:
            raise DocError(F.E_NOT_FOUND, "folder not found")
        dirs: list[Entry] = []
        files: list[Entry] = []
        for it in self.oc.list(path):
            p = it["path"].rstrip("/")
            if p == path or not p.startswith(path + "/"):
                continue
            leaf = p[len(path) + 1:]
            if not leaf or "/" in leaf:
                continue
            e = Entry(leaf, 0 if it["is_dir"] else it["size"], _unix(it["last_modified"]), it["etag"],
                      is_dir=it["is_dir"], raw_ok=True)
            (dirs if e.is_dir else files).append(e)
        dirs.sort(key=lambda e: e.name.casefold())
        files.sort(key=lambda e: e.name.casefold())
        return (dirs + files)[:MAX_LIST]

    async def dir(self, r: Reader) -> bytes:
        start, mx = r.u16(), r.u8()
        rel = wire_dir(r.str_())
        r.end()
        if start == 0:
            self.dir_snapshot = await self._call(self._dir, rel)
        return self._page(self.dir_snapshot, start, mx)

    async def mkdir(self, r: Reader) -> bytes:
        rel = wire_name(r.str_())
        r.end()
        path = f"{DOCS_ROOT}/{rel}"
        await self._call(self.oc.ensure_folder, DOCS_ROOT)
        st = await self._call(self.oc.stat, path)
        if st is not None:
            raise DocError(F.E_CONFLICT, "already exists",
                           bytes([F.CONFLICT_EXISTS]) + pstr((st["etag"] or "").encode("ascii", "replace")))
        await self._call(self.oc.mkcol, path)
        log.info("device %s made folder %s", self.device, path)
        return b""

    async def delete(self, r: Reader) -> bytes:
        rel = wire_name(r.str_())
        r.end()
        path = f"{DOCS_ROOT}/{rel}"
        if await self._call(self.oc.stat, path) is None:
            raise DocError(F.E_NOT_FOUND, "file not found")
        await self._call(self.oc.delete, path)
        log.info("device %s deleted %s", self.device, path)
        return b""

    async def transfer(self, op: int, r: Reader) -> bytes:
        from ..backend.owncloud import PreconditionFailed
        mode = r.u8()
        src = wire_name(r.str_())
        dst = wire_name(r.str_())
        r.end()
        if mode not in (0, 1):
            raise DocError(F.E_BAD_ARG, "bad mode")
        if dst == src or dst.startswith(src + "/"):
            raise DocError(F.E_BAD_ARG, "can't copy or move a folder into itself")
        sp, dp = f"{DOCS_ROOT}/{src}", f"{DOCS_ROOT}/{dst}"
        st = await self._call(self.oc.stat, sp)
        if st is None:
            raise DocError(F.E_NOT_FOUND, "file not found")
        is_dir = st["is_dir"]
        dst_st = await self._call(self.oc.stat, dp)
        if dst_st is not None and (mode == 0 or dst_st["is_dir"] != is_dir):
            # never replace a folder with a file or the other way round
            raise DocError(F.E_CONFLICT, "already exists",
                           bytes([F.CONFLICT_EXISTS]) + pstr((dst_st["etag"] or "").encode("ascii", "replace")))
        fn = self.oc.move if op == F.OP_MOVE else self.oc.copy
        try:
            await self._call(fn, sp, dp, overwrite=mode == 1, is_dir=is_dir)
        except PreconditionFailed:
            raise DocError(F.E_CONFLICT, "already exists", bytes([F.CONFLICT_EXISTS]) + pstr(b"")) from None
        log.info("device %s %s %s -> %s", self.device, F.OP_NAMES[op].lower(), sp, dp)
        return b""

    # ---- OPEN / READ
    async def open(self, r: Reader) -> bytes:
        conv = r.u8()
        name = wire_name(r.str_())
        r.end()
        t = file_type(name)
        check_conv(t, conv)
        path = f"{DOCS_ROOT}/{name}"
        st = await self._call(self.oc.stat, path)
        if st is None or st["is_dir"]:
            raise DocError(F.E_NOT_FOUND, "file not found")
        if st["size"] > MAX_FILE:
            raise DocError(F.E_TOO_LARGE, "file is larger than 1 MiB")
        data, etag = await self._call(self.oc.get, path, max_bytes=MAX_FILE)
        if len(data) > MAX_FILE:
            raise DocError(F.E_TOO_LARGE, "file is larger than 1 MiB")
        lossy = False
        if conv == CONV_RAW:
            pass
        elif t in (T_TXT, T_CSV) and conv == 0:
            data, lossy = to_wire_text(decode_utf8(data))
        elif t == T_TXT:
            data, lossy = text_to_rtf(decode_utf8(data))
        elif conv and t in (T_DOCX, T_XLSX, T_CSV):
            fn = {T_DOCX: docxconv.docx_to_rtf, T_XLSX: sheetconv.xlsx_to_simple, T_CSV: sheetconv.csv_to_simple}[t]
            try:
                data, lossy = await _convert(fn, data)
            except (ConvError, RecursionError) as e:
                msg = str(e) if isinstance(e, ConvError) else "file is nested too deeply"
                raise DocError(F.E_UNSUPPORTED, f"cannot convert: {msg}"[:120]) from None
        if len(data) > MAX_FILE:
            raise DocError(F.E_TOO_LARGE, "converted file is larger than 1 MiB")
        self.open_data = data
        etag = etag or st["etag"] or ""
        return (bytes([t, FL_LOSSY if lossy else 0]) + struct.pack("<III", len(data), F.crc32(data),
                _unix(st["last_modified"])) + pstr(etag.encode("ascii", "replace")))

    def read(self, r: Reader) -> bytes:
        off, ln = r.u32(), r.u16()
        r.end()
        if self.open_data is None:
            raise DocError(F.E_BAD_STATE, "nothing is open")
        if off > len(self.open_data):
            raise DocError(F.E_BAD_ARG, "offset beyond end of file")
        ln = min(ln, self.maxrx - 4)
        return struct.pack("<I", off) + self.open_data[off:off + ln]

    # ---- SAVE / WRITE / COMMIT
    def save_begin(self, r: Reader) -> bytes:
        mode, conv, size, crc = r.u8(), r.u8(), r.u32(), r.u32()
        name_b = r.str_()
        raw_etag = r.str_()
        r.end()
        # the etag ends up in an If-Match header: allow only what ownCloud etags look like
        if not re.fullmatch(rb'(W/)?"?[0-9A-Za-z:._-]{0,64}"?', raw_etag):
            raise DocError(F.E_BAD_ARG, "bad etag")
        etag = raw_etag.decode("ascii")
        if mode not in (0, 1) or conv not in (0, 1, 2, CONV_RAW):
            raise DocError(F.E_BAD_ARG, "bad mode/conv")
        name = wire_name(name_b, for_save=conv != CONV_RAW)    # raw copies may have any extension
        check_conv(file_type(name), conv)
        if mode == 1 and not etag:
            raise DocError(F.E_BAD_ARG, "overwrite needs the etag being replaced")
        if size > MAX_FILE:
            raise DocError(F.E_TOO_LARGE, "file is larger than 1 MiB")
        up = self.pending.get(self.device, size, crc)
        self.save = {"mode": mode, "conv": conv, "name": name, "etag": etag, "size": size, "crc": crc, "up": up}
        return struct.pack("<I", len(up.buf))

    def write(self, r: Reader) -> bytes:
        off = r.u32()
        data = r.rest()
        if self.save is None:
            raise DocError(F.E_BAD_STATE, "no SAVE in progress")
        up: Upload = self.save["up"]
        if off != len(up.buf):
            raise DocError(F.E_BAD_ARG, "offset does not match", struct.pack("<I", len(up.buf)))
        if len(up.buf) + len(data) > up.size:
            raise DocError(F.E_BAD_ARG, "more data than announced", struct.pack("<I", len(up.buf)))
        up.buf += data
        return struct.pack("<I", len(up.buf))

    async def commit(self) -> bytes:
        from ..backend.owncloud import PreconditionFailed
        s = self.save
        if s is None:
            raise DocError(F.E_BAD_STATE, "no SAVE in progress")
        up: Upload = s["up"]
        data = bytes(up.buf)
        if len(data) != s["size"] or F.crc32(data) != s["crc"]:
            self.pending.drop(self.device, s["size"], s["crc"])
            self.save = None
            raise DocError(F.E_BAD_CHECKSUM, "upload size/CRC32 mismatch; send it again")
        t, conv = file_type(s["name"]), s["conv"]
        try:
            if conv == CONV_RAW:
                pass
            elif t in (T_TXT, T_CSV) and conv == 0:
                data = data.decode(WP_CHARSET).encode("utf-8")
            elif t == T_TXT:
                data = rtf_to_text(data, WP_CHARSET).encode("utf-8")
            elif t == T_DOCX and conv == 1:
                data = await _convert(docxconv.wp_rtf_to_docx, data)
            elif t == T_XLSX and conv == 2:
                data = await _convert(sheetconv.simple_to_xlsx, data)
            elif t == T_CSV:
                data = await _convert(sheetconv.simple_to_csv, data)
        except (ValueError, RecursionError) as e:          # ConvError is a ValueError
            self.pending.drop(self.device, s["size"], s["crc"])     # retrying can't help
            self.save = None
            what = {T_TXT: "RTF could not be converted to text", T_DOCX: "RTF could not be converted to DOCX",
                    T_XLSX: "not a valid X16 sheet", T_CSV: "sheet could not be converted to CSV"}.get(t, "bad data")
            detail = f": {e}" if isinstance(e, ConvError) else ""
            raise DocError(F.E_BAD_ARG, f"{what}{detail}"[:120]) from None
        if len(data) > MAX_FILE:
            self.pending.drop(self.device, s["size"], s["crc"])
            self.save = None
            raise DocError(F.E_TOO_LARGE, "converted file is larger than 1 MiB")
        path = f"{DOCS_ROOT}/{s['name']}"
        await self._call(self.oc.ensure_folder, DOCS_ROOT)
        try:
            if s["mode"] == 0:
                etag = await self._call(self.oc.put, path, data, create_only=True)
            else:
                etag = await self._call(self.oc.put, path, data, if_match=s["etag"])
        except PreconditionFailed:
            st = await self._call(self.oc.stat, path)
            if st is None:
                raise DocError(F.E_NOT_FOUND, "file was deleted on ownCloud") from None
            kind = F.CONFLICT_EXISTS if s["mode"] == 0 else F.CONFLICT_CHANGED
            msg = "file already exists" if kind == F.CONFLICT_EXISTS else "file changed on ownCloud"
            raise DocError(F.E_CONFLICT, msg, bytes([kind]) + pstr((st["etag"] or "").encode("ascii", "replace"))) from None
        self.pending.drop(self.device, s["size"], s["crc"])
        self.save = None
        st = await self._call(self.oc.stat, path)
        etag = etag or (st or {}).get("etag") or ""
        mtime = _unix((st or {}).get("last_modified"))
        log.info("device %s saved %s (%d bytes)", self.device, path, len(data))
        return pstr(etag.encode("ascii", "replace")) + struct.pack("<II", mtime, len(data))
