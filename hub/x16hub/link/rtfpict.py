"""Pictures in .rtf files on ownCloud (docs channel, T_RTF with conv=1).

X16 Write carries a picture as a top-level {\\*\\xwpbmx HEX} group between paragraphs (HEX = the
BMX file, see bmx.py). Word and LibreOffice can't show that, so:

add_png(data, limit) -> bytes                                       (SAVE / COMMIT)
    after every depth-1 {\\*\\xwpbmx} group insert
    "\\r\\n\\pard\\plain\\qc{\\*\\xwppng}{\\pict\\pngblip\\picwW\\pichH\\picwgoalTW\\pichgoalTH HEX}\\par":
    a centered paragraph Word displays (W x H = BMX pixels, the goals = the size rule shared
    with X16 Write, in twips). X16 Write's reader skips \\pict and \\* groups. A BMX that doesn't
    parse gets no PNG; PNGs that would push the file past `limit` bytes are left out.

for_x16(data) -> (bytes, lossy)                                     (OPEN)
    * removes the paragraphs marked {\\*\\xwppng} (byte-exact undo of add_png, together with the
      CRLF before their \\pard), so an X16 RTF round-trips unchanged;
    * a paragraph with a \\pict (\\pngblip / \\jpegblip; Word's {\\*\\shppict} and floating
      {\\shp} pictures too, never the {\\nonshppict} / {\\shprslt} fallbacks) that has no
      xwpbmx group since the previous paragraph (the file was re-saved by Word, which drops
      unknown \\* groups) gets an xwpbmx group made from that picture (8 bpp, <= 320 x 240):
      a paragraph holding only pictures is replaced by the groups, one with text keeps its
      text (minus the picture groups) and the xwpbmx groups follow it. The BMX budget is the
      same as for DOCX (160,000 bytes per document, counting the xwpbmx groups already there).
      Pictures that can't be converted (WMF/EMF, bad data), pictures in tables, or past the
      budget are left as they were (X16 Write ignores them) and set lossy.
    Everything else is kept byte-for-byte.
Both are tokenizers over the bytes (no evaluation); errors leave the data unchanged.
"""
from __future__ import annotations

from . import bmx
from .ooxml import ConvError
from .rtfconv import _SKIP_DESTS, _TOKEN, MAX_DEPTH

MAX_PICTURES = 256

_BLIPS = {b"pngblip", b"jpegblip", b"emfblip", b"wmetafile", b"dibitmap", b"wbitmap", b"macpict",
          b"pmmetafile"}
_WRAPPERS = {b"pict", b"shppict", b"nonshppict", b"shp", b"shpinst", b"shprslt"}
_ALT = {b"nonshppict", b"shprslt"}             # fallback copies of a picture shown elsewhere


def _tokens(data: bytes):
    """(word, num, hexc, sym, brace, nl, text, start, end); \\binN data comes back as sym=b"BIN", text=raw bytes."""
    pos, n = 0, len(data)
    while pos < n:
        m = _TOKEN.match(data, pos)
        if m is None:
            return
        g = m.groups()
        yield (*g, m.start(), m.end())
        pos = m.end()
        if g[0] == b"bin" and g[1] is not None and int(g[1]) > 0:
            k = min(int(g[1]), n - pos)
            yield (None, None, None, b"BIN", None, None, data[pos:pos + k], pos, pos + k)
            pos += k


def _wrap_hex(raw: bytes) -> bytes:
    hx = raw.hex().encode("ascii")
    return b"\r\n".join(hx[k:k + 128] for k in range(0, len(hx), 128))


# ====================================================================== SAVE: add PNG copies

def add_png(data: bytes, limit: int | None = None) -> bytes:
    if b"\\xwpbmx" not in data:
        return data
    inserts: list[tuple[int, bytes]] = []
    extra = 0
    depth, star = 0, False
    grp = -1                    # depth of the xwpbmx group being read, -1 = none
    hexbuf: list[bytes] = []
    try:
        for word, num, hexc, sym, brace, nl, text, start, end in _tokens(data):
            if brace == b"{":
                depth += 1
                star = False
                if depth > MAX_DEPTH:
                    return data
                continue
            if brace == b"}":
                if depth == grp:
                    grp = -1
                    try:
                        raw = bytes.fromhex(b"".join(hexbuf).decode("ascii"))
                        png, w, h = bmx.bmx_to_png(raw)
                    except (ValueError, ConvError):
                        png = None
                    if png is not None:
                        tw, th = bmx.display_twips(w, h)
                        para = (b"\r\n\\pard\\plain\\qc{\\*\\xwppng}{\\pict\\pngblip\\picw%d\\pich%d"
                                b"\\picwgoal%d\\pichgoal%d " % (w, h, tw, th) + _wrap_hex(png) + b"}\\par")
                        if limit is None or len(data) + extra + len(para) <= limit:
                            inserts.append((end, para))
                            extra += len(para)
                depth -= 1
                star = False
                continue
            if sym == b"*":
                star = True
                continue
            if word is not None:
                if star and word == b"xwpbmx" and depth == 2 and grp < 0:
                    grp, hexbuf = depth, []
                star = False
                continue
            if text is not None and depth == grp:
                hexbuf.append(text)
    except RecursionError:
        return data
    if not inserts:
        return data
    out, last = bytearray(), 0
    for pos, ins in inserts:
        out += data[last:pos] + ins
        last = pos
    out += data[last:]
    return bytes(out)


# ====================================================================== OPEN: back to X16 Write

class _Group:
    __slots__ = ("start", "kind", "star", "hidden", "foreign", "parent_wrapper", "pict", "blip", "hexbuf", "binbuf")

    def __init__(self, start: int, parent: "_Group | None"):
        self.start = start
        self.kind: bytes | None = None
        self.star = False
        self.hidden = parent.hidden if parent else False
        self.foreign = parent.foreign if parent else False
        self.parent_wrapper = bool(parent and (parent.kind in _WRAPPERS or parent.parent_wrapper))
        self.pict = False
        self.blip: bytes | None = None
        self.hexbuf: list[bytes] = []
        self.binbuf: list[bytes] = []


class _Para:
    def __init__(self, start: int):
        self.start = start
        self.pard: int | None = None              # the first depth-1 \pard
        self.text = False
        self.pics: list[tuple[bytes | None, bytes]] = []    # (blip kind, picture bytes) to convert
        self.unsupported = False                  # a \pict we can't convert
        self.wrappers: list[tuple[int, int]] = []
        self.intbl = False
        self.xwppng = False
        self.bmx_before = False                   # an xwpbmx group since the previous paragraph


def _bmx_bytes_present(data: bytes) -> int:
    total, depth, star, grp = 0, 0, False, -1
    for word, num, hexc, sym, brace, nl, text, start, end in _tokens(data):
        if brace == b"{":
            depth += 1
            star = False
        elif brace == b"}":
            if depth == grp:
                grp = -1
            depth -= 1
            star = False
        elif sym == b"*":
            star = True
        elif word is not None:
            if star and word == b"xwpbmx" and grp < 0:
                grp = depth
            star = False
        elif text is not None and depth == grp:
            total += len(text.strip()) // 2
    return total


def for_x16(data: bytes) -> tuple[bytes, bool]:
    if b"\\pict" not in data:
        return data, False
    try:
        return _for_x16(data)
    except (RecursionError, ValueError):
        return data, False


def _for_x16(data: bytes) -> tuple[bytes, bool]:
    budget = bmx.DOC_BUDGET - _bmx_bytes_present(data)
    full, tried = budget < 0, 0
    lossy = False
    edits: list[tuple[int, int, bytes]] = []       # (start, end, replacement)
    pending: bytes | None = None                   # groups to insert once back at depth 1
    stack: list[_Group] = []
    star = False
    para = _Para(0)

    def convert(p: _Para) -> list[bytes]:
        nonlocal budget, full, tried, lossy
        out = []
        for kind, raw in p.pics:
            if full or tried >= MAX_PICTURES:
                lossy = full = True
                break
            tried += 1
            try:
                b = bmx.picture_to_bmx(raw)
            except ConvError:
                lossy = True
                continue
            if len(b) > budget:
                lossy = full = True
                break
            budget -= len(b)
            out.append(bmx.rtf_group(b))
        return out

    def end_para(end: int, depth: int, table: bool):
        nonlocal para, pending, lossy
        p = para
        para = _Para(end)
        removable = p.pard is not None and depth == 1 and not p.text
        if p.xwppng:
            if p.pard is not None and depth == 1:
                s = p.pard - 2 if data[p.pard - 2:p.pard] == b"\r\n" else p.pard
                edits.append((s, end, b""))
            return
        if p.unsupported:
            lossy = True
        if not p.pics:
            return
        if table or p.intbl:
            lossy = True
            return
        if p.bmx_before:                            # the X16 copy is already there
            if removable:
                edits.append((p.pard, end, b""))
            return
        groups = convert(p)
        if not groups:
            return
        if removable:
            edits.append((p.pard, end, b"\r\n".join(groups)))
            return
        for s, e in p.wrappers:
            edits.append((s, e, b""))
        ins = b"\r\n" + b"\r\n".join(groups)
        if depth == 1:
            edits.append((end, end, ins))
        else:
            pending = ins

    for word, num, hexc, sym, brace, nl, text, start, end in _tokens(data):
        top = stack[-1] if stack else None
        if brace == b"{":
            if len(stack) >= MAX_DEPTH:
                return data, False
            stack.append(_Group(start, top))
            star = False
            continue
        if brace == b"}":
            star = False
            if not stack:
                continue
            g = stack.pop()
            if g.pict and not g.foreign:
                if g.blip in (b"pngblip", b"jpegblip"):
                    try:
                        raw = b"".join(g.binbuf) or bytes.fromhex(b"".join(g.hexbuf).decode("ascii"))
                        para.pics.append((g.blip, raw))
                    except ValueError:
                        para.unsupported = True
                else:
                    para.unsupported = True
            if g.kind in _WRAPPERS and not g.parent_wrapper:
                para.wrappers.append((g.start, end))
            if g.kind == b"xwpbmx" and g.star:
                para.bmx_before = True
            if len(stack) == 1 and pending is not None:
                edits.append((end, end, pending))
                pending = None
            continue
        if nl:
            continue
        if sym is not None:
            if sym == b"*":
                star = True
            elif sym == b"BIN":
                if top is not None and top.pict:
                    top.binbuf.append(text)
            elif top is not None and not top.hidden and sym in (b"\\", b"{", b"}", b"~", b"_"):
                para.text = True
            continue
        if word is not None:
            if top is not None and top.kind is None:
                top.kind, top.star = word, star
                if star or word in _SKIP_DESTS or word in _WRAPPERS:
                    top.hidden = True
                if (star and word not in _WRAPPERS) or (word in _SKIP_DESTS and word != b"pict") or word in _ALT:
                    top.foreign = True
                if word == b"pict":
                    top.pict = True
                if star and word == b"xwppng":
                    para.xwppng = True
            star = False
            if top is not None and top.pict and word in _BLIPS:
                top.blip = word
            if top is not None and top.hidden:
                continue
            depth = len(stack)
            if word == b"par":
                end_para(end, depth, False)
            elif word in (b"cell", b"row", b"nestcell", b"nestrow"):
                end_para(end, depth, True)
            elif word == b"pard":
                para.intbl = False
                if depth == 1 and para.pard is None:
                    para.pard = start
            elif word == b"intbl":
                para.intbl = True
            elif word in (b"u", b"tab", b"bullet", b"emdash", b"endash", b"lquote", b"rquote",
                          b"ldblquote", b"rdblquote"):
                para.text = True
            continue
        if hexc is not None:
            if top is not None and not top.hidden:
                para.text = True
            continue
        if text is not None and top is not None:
            if top.pict:
                top.hexbuf.append(text)
            elif not top.hidden and text.strip():
                para.text = True
    if para.pics or para.unsupported:
        lossy = True                                # pictures after the last \par
    if not edits:
        return data, lossy
    edits.sort(key=lambda e: (e[0], e[1]))
    out, last = bytearray(), 0
    for s, e, rep in edits:
        if s < last:                                # overlapping edits: keep the first
            continue
        out += data[last:s] + rep
        last = e
    out += data[last:]
    return bytes(out), lossy
