"""Text <-> RTF conversion for the X16 WP (docs channel, conv=1).

* text_to_rtf: plain text (str) -> minimal RTF the WP's importer reads
  (one paragraph per line, ISO-8859-15 escapes, tabs kept as \\tab).
* rtf_to_text: RTF (bytes) -> str. Understands the WP's own dialect
  ({\\*\\xwp N} paragraph tags: bit 7 = soft-wrapped continuation line, rejoined
  with a space) and plain RTF from elsewhere. Only text survives.

Both are bounded: input is limited by the caller (1 MiB), group depth is capped,
and nothing is evaluated -- it's a tokenizer, not an interpreter.
"""
from __future__ import annotations

import re

WP_CHARSET = "iso8859_15"

_HEADER = (
    "{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033\r\n"
    "{\\fonttbl{\\f0\\fmodern\\fcharset0 Courier New;}}\r\n"
    "\\pard\\plain\\fs24 "
)


def to_wire_text(s: str) -> tuple[bytes, bool]:
    """str -> ISO-8859-15 bytes, '?' for unmappable chars. Returns (bytes, lossy)."""
    b = s.encode(WP_CHARSET, "replace")
    return b, b.decode(WP_CHARSET) != s


def decode_utf8(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")  # legacy text file: best effort


# Typographic chars outside ISO-8859-15 -> closest ASCII (instead of '?').
_FOLD = str.maketrans({"\u2013": "-", "\u2014": "--", "\u2018": "'", "\u2019": "'",
                       "\u201a": ",", "\u201c": '"', "\u201d": '"', "\u201e": '"',
                       "\u2026": "...", "\u2022": "*", "\u00a0": " ", "\u2212": "-",
                       "\u2010": "-", "\u2011": "-", "\u2032": "'", "\u2033": '"'})


def text_to_rtf(text: str) -> tuple[bytes, bool]:
    text = text.translate(_FOLD)
    raw, lossy = to_wire_text(text.replace("\r\n", "\n").replace("\r", "\n"))
    out = bytearray(_HEADER.encode("ascii"))
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for line in lines:
        for c in line:
            if c in (0x5C, 0x7B, 0x7D):          # \ { }
                out += b"\\" + bytes([c])
            elif c == 9:
                out += b"\\tab "
            elif c < 32:
                continue
            elif c >= 128:
                out += b"\\'%02x" % c
            else:
                out.append(c)
        out += b"\\par\r\n"
    out += b"}\r\n"
    return bytes(out), lossy


_SKIP_DESTS = {b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"header", b"footer",
               b"headerl", b"headerr", b"footerl", b"footerr", b"listtable", b"listoverridetable",
               b"rsidtbl", b"generator", b"xmlnstbl", b"themedata", b"colorschememapping",
               b"latentstyles", b"datastore", b"object", b"field", b"fldinst", b"pntext", b"listtext"}
_TOKEN = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([\r\n]+)|([^\\{}\r\n]+)", re.S)
MAX_DEPTH = 64


def rtf_to_text(data: bytes, charset: str | None = None) -> str:
    """charset of \\'xx escapes: the X16 WP writes raw ISO-8859-15 bytes (despite
    declaring ansicpg1252), so RTF coming from the X16 must be decoded with WP_CHARSET.
    None = guess (WP dialect -> ISO-8859-15, else cp1252)."""
    if charset is None:
        charset = WP_CHARSET if b"\\*\\xwp" in data else "cp1252"
    paras: list[tuple[bool, list[str]]] = []      # (soft continuation, chars)
    cur: list[str] = []
    cur_soft = False
    stack: list[tuple[bool, int]] = []
    skip = False
    uc = 1
    pending_skip = 0
    star = False
    for m in _TOKEN.finditer(data):
        word, num, hexc, sym, brace, _nl, text = m.groups()
        if brace == b"{":
            if len(stack) >= MAX_DEPTH:
                raise ValueError("RTF nested too deeply")
            stack.append((skip, uc))
            star = False
            continue
        if brace == b"}":
            if stack:
                skip, uc = stack.pop()
            star = False
            continue
        if _nl:
            continue
        if sym is not None:
            if sym == b"*":
                star = True
                continue
            if skip:
                continue
            if sym in (b"\\", b"{", b"}"):
                cur.append(sym.decode())
            elif sym == b"~":
                cur.append("\u00a0")
            elif sym in (b"-", b"_"):
                cur.append("-" if sym == b"_" else "")
            continue
        if hexc is not None:
            if pending_skip:
                pending_skip -= 1
                continue
            if not skip:
                cur.append(bytes([int(hexc, 16)]).decode(charset, "replace"))
            continue
        if word is not None:
            if star:
                star = False
                if word == b"xwp" and num is not None and not skip:
                    cur_soft = bool(int(num) & 128)
                skip = True
                continue
            if word in _SKIP_DESTS:
                skip = True
                continue
            if skip:
                continue
            if word in (b"par", b"line", b"page", b"sect"):
                paras.append((cur_soft, cur))
                cur, cur_soft = [], False
            elif word == b"pard":
                cur_soft = False
            elif word == b"tab":
                cur.append("\t")
            elif word == b"uc" and num is not None:
                uc = max(0, min(int(num), 4))
            elif word == b"u" and num is not None:
                v = int(num)
                if v < 0:
                    v += 65536
                if 0 < v < 0x110000:
                    cur.append(chr(v) if not 0xD800 <= v <= 0xDFFF else "?")
                pending_skip = uc
            continue
        if text is not None:
            if skip:
                continue
            t = text
            if pending_skip:
                n = min(pending_skip, len(t))
                t, pending_skip = t[n:], pending_skip - n
            cur.append(t.decode(charset, "replace"))
    if cur:
        paras.append((cur_soft, cur))
    lines: list[str] = []
    for soft, chars in paras:
        s = "".join(chars).rstrip(" ")
        if soft and lines:
            prev = lines[-1]
            lines[-1] = prev + ("" if prev.endswith(" ") or not prev else " ") + s.lstrip(" ")
        else:
            lines.append(s)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")
