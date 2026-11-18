"""X16Link v1 framing (see ~/x16-office/PROTOCOL.md).

Frame: [len u16 LE][chan][seq][op][payload * len][crc16 LE]
CRC-16/CCITT-FALSE over everything before the CRC.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

VERSION = 1
MAGIC = b"X16L"
HDR = 5                  # len(2) chan seq op
TRAILER = 2              # crc16
ABS_MAX_PAYLOAD = 1024   # never more than this, whatever the peer announces
PREAUTH_MAX_PAYLOAD = 64

# channels
CH_CTRL, CH_DOCS, CH_CAL = 0, 1, 2

# control ops
OP_HELLO, OP_AUTH, OP_NOOP, OP_TIME, OP_BYE = 0x01, 0x02, 0x03, 0x04, 0x05
OP_ERR = 0x7F
# docs ops
OP_LIST, OP_OPEN, OP_READ, OP_SAVE, OP_WRITE, OP_COMMIT = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15
# docs ops for folders (file manager, Save As folder browser)
OP_DIR, OP_MKDIR, OP_DELETE, OP_MOVE, OP_COPY = 0x16, 0x17, 0x18, 0x19, 0x1A
# calendar ops (channel 2)
OP_CAL_BEGIN, OP_CAL_PUT, OP_CAL_SYNC, OP_CAL_GET = 0x30, 0x31, 0x32, 0x33
OP_CAL_CONFLICTS, OP_CAL_ACK, OP_CAL_STATUS = 0x34, 0x35, 0x36
REPLY = 0x80

OP_NAMES = {
    OP_HELLO: "HELLO", OP_AUTH: "AUTH", OP_NOOP: "NOOP", OP_TIME: "TIME", OP_BYE: "BYE",
    OP_ERR: "ERR", OP_LIST: "LIST", OP_OPEN: "OPEN", OP_READ: "READ", OP_SAVE: "SAVE",
    OP_WRITE: "WRITE", OP_COMMIT: "COMMIT", OP_DIR: "DIR", OP_MKDIR: "MKDIR", OP_DELETE: "DELETE",
    OP_MOVE: "MOVE", OP_COPY: "COPY", OP_CAL_BEGIN: "CAL_BEGIN", OP_CAL_PUT: "CAL_PUT",
    OP_CAL_SYNC: "CAL_SYNC", OP_CAL_GET: "CAL_GET", OP_CAL_CONFLICTS: "CAL_CONFLICTS",
    OP_CAL_ACK: "CAL_ACK", OP_CAL_STATUS: "CAL_STATUS",
}

# error codes
E_BAD_FRAME, E_BAD_CRC, E_TOO_BIG, E_BAD_OP, E_BAD_STATE, E_AUTH_FAIL = 1, 2, 3, 4, 5, 6
E_NOT_FOUND, E_CONFLICT, E_BAD_NAME, E_UNSUPPORTED, E_BACKEND, E_BAD_ARG = 7, 8, 9, 10, 11, 12
E_BAD_CHECKSUM, E_BUSY, E_TIMEOUT, E_TOO_LARGE = 13, 14, 15, 16

# conflict kinds
CONFLICT_EXISTS, CONFLICT_CHANGED = 1, 2


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF). crc16(b'123456789') == 0x29B1."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass(frozen=True)
class Frame:
    chan: int
    seq: int
    op: int
    payload: bytes = b""

    def encode(self) -> bytes:
        return encode(self.chan, self.seq, self.op, self.payload)

    @property
    def name(self) -> str:
        base = OP_NAMES.get(self.op & 0x7F, f"op{self.op:#04x}")
        return base + ("'" if self.op & REPLY else "")


def encode(chan: int, seq: int, op: int, payload: bytes = b"") -> bytes:
    if len(payload) > ABS_MAX_PAYLOAD:
        raise ValueError("payload too large")
    head = struct.pack("<HBBB", len(payload), chan & 0xFF, seq & 0xFF, op & 0xFF) + payload
    return head + struct.pack("<H", crc16(head))


class FrameError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def decode(buf: bytes, max_payload: int = ABS_MAX_PAYLOAD) -> tuple[Frame, int]:
    """Decode one frame from the start of buf. Returns (frame, bytes_used).

    Raises FrameError(E_TOO_BIG/E_BAD_CRC) or IndexError-free ValueError('short') when
    more bytes are needed.
    """
    if len(buf) < 2:
        raise ValueError("short")
    (n,) = struct.unpack_from("<H", buf)
    if n > min(max_payload, ABS_MAX_PAYLOAD):
        raise FrameError(E_TOO_BIG, "frame too big")
    total = HDR + n + TRAILER
    if len(buf) < total:
        raise ValueError("short")
    body = bytes(buf[: HDR + n])
    (crc,) = struct.unpack_from("<H", buf, HDR + n)
    if crc16(body) != crc:
        raise FrameError(E_BAD_CRC, "bad crc")
    return Frame(body[2], body[3], body[4], body[HDR:]), total


# --------------------------------------------------------------------------- payload helpers

class Reader:
    """Bounds-checked payload reader; every short read is a FrameError(BAD_FRAME)."""

    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.d):
            raise FrameError(E_BAD_FRAME, "payload too short")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def raw(self, n: int) -> bytes:
        return self._take(n)

    def str_(self) -> bytes:
        return self._take(self.u8())

    def rest(self) -> bytes:
        b = self.d[self.i:]
        self.i = len(self.d)
        return b

    def end(self) -> None:
        if self.i != len(self.d):
            raise FrameError(E_BAD_FRAME, "trailing bytes in payload")


def pstr(b: bytes | str) -> bytes:
    if isinstance(b, str):
        b = b.encode("iso8859_15", "replace")
    b = b[:255]
    return bytes([len(b)]) + b


def err_payload(code: int, msg: str, extra: bytes = b"") -> bytes:
    return bytes([code]) + pstr(msg[:120]) + extra
