"""Shared helpers for the OOXML converters (sheetconv, docxconv).

* bounded ZIP reading (zip-bomb guard: entry count, declared and actual
  uncompressed sizes are capped; encrypted / unsupported entries are refused),
* hostile-XML-safe parsing (defusedxml),
* deterministic ZIP writers (stored = the X16's own layout, deflated = ownCloud).

Every failure surfaces as ConvError (a ValueError) with a short, user-facing
message; callers turn it into a protocol error.
"""
from __future__ import annotations

import io
import struct
import zipfile
import zlib
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring


class ConvError(ValueError):
    """Conversion failed; str(e) is safe to show to the user."""


MAX_ENTRIES = 2000              # entries in one archive
MAX_TOTAL = 96 << 20            # sum of declared uncompressed sizes
PART_CAP = 32 << 20             # one big part (sheet / document / sharedStrings)
SMALL_CAP = 4 << 20             # rels, workbook, styles, numbering, ...


def open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError, struct.error):
        raise ConvError("not a valid ZIP (OOXML) file") from None
    if len(infos) > MAX_ENTRIES:
        raise ConvError("ZIP has too many entries")
    if sum(i.file_size for i in infos) > MAX_TOTAL:
        raise ConvError("ZIP expands to too much data")
    return zf


def _find(zf: zipfile.ZipFile, name: str) -> zipfile.ZipInfo | None:
    name = name.lstrip("/")
    try:
        return zf.getinfo(name)
    except KeyError:
        low = name.casefold()
        for i in zf.infolist():           # OPC part names are case-insensitive
            if i.filename.casefold() == low:
                return i
    return None


def read_part(zf: zipfile.ZipFile, name: str, cap: int = SMALL_CAP) -> bytes | None:
    """Bytes of one entry (None if missing). Never inflates more than cap+1 bytes."""
    info = _find(zf, name)
    if info is None:
        return None
    if info.file_size > cap:
        raise ConvError("a part of the file is too large to convert")
    try:
        with zf.open(info) as f:
            b = f.read(cap + 1)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, OSError, EOFError, ValueError):
        raise ConvError("damaged, encrypted or unsupported compression") from None
    if len(b) > cap:
        raise ConvError("a part of the file is too large to convert")
    return b


def parse_xml(b: bytes, what: str = "XML"):
    try:
        return _safe_fromstring(b)
    except (ParseError, DefusedXmlException, ValueError, SyntaxError):
        raise ConvError(f"{what} is not valid or uses forbidden XML features") from None


def rels_targets(zf: zipfile.ZipFile, rels_path: str, base_dir: str) -> dict[str, tuple[str, str]]:
    """rId -> (type suffix, absolute part name) from a .rels part."""
    b = read_part(zf, rels_path)
    out: dict[str, tuple[str, str]] = {}
    if b is None:
        return out
    root = parse_xml(b, "relationships")
    for rel in root:
        if not rel.tag.endswith("}Relationship") or rel.get("TargetMode") == "External":
            continue
        tgt = rel.get("Target") or ""
        out[rel.get("Id") or ""] = ((rel.get("Type") or "").rsplit("/", 1)[-1], resolve(base_dir, tgt))
    return out


def resolve(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        parts = target.lstrip("/").split("/")
    else:
        parts = [p for p in base_dir.split("/") if p] + target.split("/")
    out: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            if out:
                out.pop()
            continue
        out.append(p)
    return "/".join(out)


def office_document(zf: zipfile.ZipFile, default: str) -> str:
    for typ, tgt in rels_targets(zf, "_rels/.rels", "").values():
        if typ == "officeDocument":
            return tgt
    return default


def esc_ascii(s: str) -> str:
    """XML text/attribute escape, ASCII only: the 5 entities, everything else non-ASCII as &#N;."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == '"':
            out.append("&quot;")
        elif ch == "'":
            out.append("&apos;")
        elif o >= 128:
            out.append(f"&#{o};")
        else:
            out.append(ch)
    return "".join(out)


_DOS_DATE = (0 << 9) | (1 << 5) | 1          # 1980-01-01
_DOS_TIME = 0


def stored_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Deterministic stored ZIP: no data descriptors, extras, comments or ZIP64."""
    out = bytearray()
    central = bytearray()
    for name, data in entries:
        nb = name.encode("ascii")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        off = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 10, 0, 0, _DOS_TIME, _DOS_DATE, crc,
                           len(data), len(data), len(nb), 0) + nb + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 10, 10, 0, 0, _DOS_TIME, _DOS_DATE, crc,
                               len(data), len(data), len(nb), 0, 0, 0, 0, 0, off) + nb
    cd_off = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), cd_off, 0)
    return bytes(out)


def deflated_zip(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in entries:
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, data)
    return buf.getvalue()
