"""X16 Write <-> DOCX conversion (docs channel, conv=1 on .docx).

wp_rtf_to_docx(data) -> bytes
    RTF from the X16 WP (its line-per-paragraph dialect, see commanderx16wp/rtf.p8
    export_line) -> a minimal DOCX. {\\*\\xwp V} tags drive the structure
    (V = align | list<<2 | indent<<4, bit 7 = soft-wrapped continuation, joined
    back into one paragraph with a space). List markers (\\u9679? / \\u9675? /
    \\u9632? + 3 spaces, or "N." padded to 4 chars) are stripped and become real
    Word lists (bullet numId 1; a numbered run gets its own w:num with a
    startOverride when its first number isn't the continuation of the previous
    numbered run at that level). Runs keep bold/italic/underline, the font
    (fonttbl name), text colour (colortbl) and highlight (w:shd fill).
    Generic RTF (no xwp tags) is converted best-effort: paragraphs with
    alignment, \\li / \\fi indents, \\page / \\pagebb page breaks and the same run
    formatting; no list detection.
    Tables (\\trowd .. \\cellx .. \\intbl .. \\cell .. \\row, as X16 Write writes them, up to
    8 x 20) become a w:tbl with single borders, the \\cellx widths, \\trhdr as a header
    row; a paragraph follows every table (Word needs one).

docx_to_rtf(data) -> (bytes, lossy)
    Any DOCX -> RTF the WP importer reads: the WP fonttbl and 16-colour palette,
    one \\pard\\plain ... \\par per paragraph (no xwp tags, the WP re-wraps),
    \\qc/\\qr/\\ql, \\li level*576, \\b \\i \\ul \\fN \\cfN \\highlightN, font sizes as the
    nearest of the WP's 12/18/24/36 pt (\\fs24/36/48/72), \\'xx in
    ISO-8859-15 ('?' + lossy for anything else, typographic punctuation folded).
    Lists become text markers; page breaks become \\pagebb on the following
    paragraph (the WP importer ignores \\page as a paragraph break); a manual line
    break starts a new paragraph with the same formatting; tables become X16 Write
    tables (\\trowd .. \\cell .. \\row with equal \\cellx widths, the first row \\trhdr):
    a cell's paragraphs are joined with spaces, more than 8 columns are merged into
    the 8th and a cell longer than the WP's cell width is cut by the WP (lossy),
    nested tables are flattened into their cell (lossy); images/objects/footnotes
    are dropped (lossy); paragraphs longer than the WP's 1024-char buffer are split.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as _xesc

from .ooxml import (PART_CAP, ConvError, deflated_zip, office_document, open_zip, parse_xml, read_part,
                    rels_targets)
from .rtfconv import _FOLD, _SKIP_DESTS, _TOKEN, MAX_DEPTH, WP_CHARSET

__all__ = ["wp_rtf_to_docx", "docx_to_rtf", "WP_PALETTE", "ConvError"]

# X16 Write palette (vid.p8 pal_hi/pal_lo, 4-bit components * 17), colortbl entries 1..16
_PAL_LO = [0x00, 0xff, 0x00, 0xfe, 0x4c, 0xc5, 0x0a, 0xe7, 0x85, 0x40, 0x77, 0x33, 0x77, 0xf6, 0x8f, 0xbb]
_PAL_HI = [0x0, 0xf, 0x8, 0xa, 0xc, 0x0, 0x0, 0xe, 0xd, 0x6, 0xf, 0x3, 0x7, 0x8, 0x0, 0xb]
WP_PALETTE = [(h * 17, (lo >> 4) * 17, (lo & 15) * 17) for h, lo in zip(_PAL_HI, _PAL_LO)]
WP_FONTS = ["Courier New", "Lucida Console", "Andale Mono"]
LIST_INDENT = 576                       # twips per WP indent level (4 columns of 10 cpi)
WP_PARA_MAX = 1000                      # WP paragraph buffer is 1024 chars

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = "{%s}" % W

# ====================================================================== RTF -> DOCX


@dataclass
class _Para:
    runs: list = field(default_factory=list)      # [props tuple, text]
    align: str = "left"
    li: int = 0
    fi: int = 0
    pagebb: bool = False
    xwp: int | None = None
    row: list | None = None                       # a table row: cells, each a list of paragraphs (runs)
    cellx: list = field(default_factory=list)     # right edge of every cell (twips)
    header: bool = False                          # \trhdr


# run props: (b, i, u, font, cf, highlight, fs)
def _default_props(deff: int) -> tuple:
    return (False, False, False, deff, 0, 0, 24)


def _parse_rtf(data: bytes):
    if not data.lstrip()[:5] == b"{\\rtf":
        raise ConvError("not an RTF document")
    charset = WP_CHARSET if b"\\*\\xwp" in data else "cp1252"
    m = re.search(rb"\\deff(\d+)", data[:2000])
    deff = int(m.group(1)) if m else 0
    paras: list[_Para] = []
    fonts: dict[int, str] = {}
    colors: list[tuple[int, int, int] | None] = []
    page: dict[str, int] = {}
    props = list(_default_props(deff))
    pp = _Para()
    stack: list[tuple[list, str | None, int]] = []
    dest: str | None = None
    uc, pending_skip, star = 1, 0, False
    font_cur, font_name = None, []
    comps: dict[str, int] = {}
    pending_page = False
    in_row, cellx_done = False, False             # table row being read; its \cellx list is complete
    row_cells: list = []
    cell_paras: list = []
    row_cellx: list[int] = []
    row_hdr = False

    def add(text: str):
        if not text:
            return
        p = tuple(props)
        if pp.runs and pp.runs[-1][0] == p:
            pp.runs[-1][1] += text
        else:
            pp.runs.append([p, text])

    def finish():
        nonlocal pp, pending_page
        if in_row:                                # a paragraph inside a table cell
            cell_paras.append(pp.runs)
            pp = _Para()
            return
        done = pp
        done.pagebb = done.pagebb or pending_page
        pending_page = False
        paras.append(done)
        pp = _Para(align=done.align, li=done.li, fi=done.fi)      # \pagebb and xwp are one-shot

    def fontname_text(t: str):
        nonlocal font_name
        for ch in t:
            if ch == ";":
                if font_cur is not None:
                    fonts[font_cur] = "".join(font_name).strip()
                font_name = []
            else:
                font_name.append(ch)

    for mt in _TOKEN.finditer(data):
        word, num, hexc, sym, brace, nl, text = mt.groups()
        if brace == b"{":
            if len(stack) >= MAX_DEPTH:
                raise ConvError("RTF nested too deeply")
            stack.append((list(props), dest, uc))
            star = False
            continue
        if brace == b"}":
            if stack:
                props, dest, uc = stack.pop()
            star = False
            continue
        if nl:
            continue
        if sym is not None:
            if sym == b"*":
                star = True
                continue
            if dest == "fonttbl":
                if sym in (b"\\", b"{", b"}"):
                    fontname_text(sym.decode())
                continue
            if dest is not None:
                continue
            if sym in (b"\\", b"{", b"}"):
                add(sym.decode())
            elif sym == b"~":
                add("\u00a0")
            elif sym == b"_":
                add("-")
            elif sym in (b"\r", b"\n"):
                finish()
            continue
        if hexc is not None:
            if pending_skip:
                pending_skip -= 1
                continue
            ch = bytes([int(hexc, 16)]).decode(charset, "replace")
            if dest is None:
                add(ch)
            elif dest == "fonttbl":
                fontname_text(ch)
            continue
        if word is not None:
            w = word.decode()
            n = int(num) if num is not None else None
            if star:
                star = False
                if w == "xwp" and n is not None and dest is None:
                    pp.xwp = n
                dest = "skip"
                continue
            if w in ("fonttbl", "colortbl"):
                dest = w
                continue
            if word in _SKIP_DESTS:
                dest = "skip"
                continue
            if dest == "skip":
                continue
            if dest == "fonttbl":
                if w == "f" and n is not None:
                    font_cur, font_name = n, []
                continue
            if dest == "colortbl":
                if w in ("red", "green", "blue") and n is not None:
                    comps[w] = max(0, min(255, n))
                continue
            on = n != 0
            if w in ("trowd", "intbl") and not in_row:
                if pp.runs:
                    finish()
                pp = _Para()
                in_row, cellx_done, row_cells, cell_paras, row_cellx, row_hdr = True, False, [], [], [], False
                if w == "trowd":
                    continue
            if w == "trowd":
                cellx_done = cellx_done or bool(row_cells)    # Word repeats the row definition at its end
            elif w == "cellx" and in_row and not cellx_done and n is not None:
                row_cellx.append(n)
            elif w == "trhdr" and in_row:
                row_hdr = True
            elif w == "cell" and in_row:
                cell_paras.append(pp.runs)
                row_cells.append(cell_paras)
                cell_paras = []
                pp = _Para()
            elif w == "row" and in_row:
                if pp.runs or cell_paras:
                    cell_paras.append(pp.runs)
                    row_cells.append(cell_paras)
                paras.append(_Para(row=row_cells, cellx=row_cellx, header=row_hdr))
                in_row, cell_paras = False, []
                pp = _Para()
            elif w == "par":
                finish()
            elif w == "pard":
                pp.align, pp.li, pp.fi, pp.pagebb, pp.xwp = "left", 0, 0, False, None
            elif w == "plain":
                props = list(_default_props(deff))
            elif w == "b":
                props[0] = on
            elif w == "i":
                props[1] = on
            elif w in ("ul", "ulw", "uld", "uldb", "ulth", "uldash", "ulwave"):
                props[2] = on
            elif w == "ulnone":
                props[2] = False
            elif w == "f" and n is not None:
                props[3] = n
            elif w == "cf":
                props[4] = n or 0
            elif w in ("highlight", "cb"):
                props[5] = n or 0
            elif w == "fs" and n:
                props[6] = n
            elif w in ("ql", "qc", "qr", "qj"):
                pp.align = {"ql": "left", "qc": "center", "qr": "right", "qj": "both"}[w]
            elif w == "li":
                pp.li = n or 0
            elif w == "fi":
                pp.fi = n or 0
            elif w == "pagebb":
                pp.pagebb = True
            elif w == "page":
                if pp.runs:
                    finish()
                pending_page = True
            elif w == "line":
                add("\n")
            elif w == "tab":
                add("\t")
            elif w in ("emdash", "endash", "bullet", "lquote", "rquote", "ldblquote", "rdblquote"):
                add({"emdash": "\u2014", "endash": "\u2013", "bullet": "\u2022", "lquote": "\u2018",
                     "rquote": "\u2019", "ldblquote": "\u201c", "rdblquote": "\u201d"}[w])
            elif w == "uc" and n is not None:
                uc = max(0, min(n, 4))
            elif w == "u" and n is not None:
                v = n + 65536 if n < 0 else n
                if 0 < v < 0x110000 and not 0xD800 <= v <= 0xDFFF:
                    add(chr(v))
                pending_skip = uc
            elif w in ("paperw", "paperh", "margl", "margr", "margt", "margb") and n is not None and n > 0:
                page[w] = n
            continue
        if text is not None:
            if dest == "colortbl":
                for _ in range(text.count(b";")):
                    colors.append((comps.get("red", 0), comps.get("green", 0), comps.get("blue", 0))
                                  if comps else None)
                    comps = {}
                continue
            t = text
            if pending_skip:
                k = min(pending_skip, len(t))
                t, pending_skip = t[k:], pending_skip - k
            if dest == "fonttbl":
                fontname_text(t.decode(charset, "replace"))
            elif dest is None and t:
                add(t.decode(charset, "replace"))
    if in_row and (row_cells or pp.runs):         # a row without its \row
        if pp.runs:
            cell_paras.append(pp.runs)
            row_cells.append(cell_paras)
        paras.append(_Para(row=row_cells, cellx=row_cellx, header=row_hdr))
        pp = _Para()
    if pp.runs:
        finish()
    return paras, fonts, colors, page, deff


def _strip_chars(runs: list, n: int) -> tuple[str, list]:
    taken = ""
    out = []
    for p, t in runs:
        if n > 0:
            k = min(n, len(t))
            taken += t[:k]
            t, n = t[k:], n - k
        if t:
            out.append([p, t])
    return taken, out


def _hex(c) -> str:
    return "%02X%02X%02X" % c


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _run_xml(props: tuple, text: str, fonts, colors, deff) -> str:
    b, i, u, f, cf, hl, fs = props
    rpr = []
    if f != deff and fonts.get(f):
        nm = _xesc(fonts[f], {'"': "&quot;"})
        rpr.append(f'<w:rFonts w:ascii="{nm}" w:hAnsi="{nm}" w:cs="{nm}"/>')
    if b:
        rpr.append("<w:b/>")
    if i:
        rpr.append("<w:i/>")
    col = colors[cf] if 0 < cf < len(colors) else None
    if col is not None and col != (0, 0, 0):
        rpr.append(f'<w:color w:val="{_hex(col)}"/>')
    if fs != 24 and 2 <= fs <= 3276:
        rpr.append(f'<w:sz w:val="{fs}"/><w:szCs w:val="{fs}"/>')
    if u:
        rpr.append('<w:u w:val="single"/>')
    bg = colors[hl] if 0 < hl < len(colors) else None
    if bg is not None and bg != (255, 255, 255):
        rpr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{_hex(bg)}"/>')
    head = "<w:r>" + (f"<w:rPr>{''.join(rpr)}</w:rPr>" if rpr else "")
    body = []
    for part in re.split(r"([\t\n])", _XML_BAD.sub("", text)):
        if part == "\t":
            body.append("<w:tab/>")
        elif part == "\n":
            body.append("<w:br/>")
        elif part:
            body.append(f'<w:t xml:space="preserve">{_xesc(part)}</w:t>')
    return head + "".join(body) + "</w:r>" if body else ""


_CT_DOCX = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    '<Override PartName="/docProps/app.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    '</Types>')
_RELS_DOCX = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'extended-properties" Target="docProps/app.xml"/></Relationships>')
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'styles" Target="styles.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'numbering" Target="numbering.xml"/></Relationships>')
_APP = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>X16 Office hub</Application></Properties>')


def _styles_xml(font: str) -> str:
    nm = _xesc(font or "Courier New", {'"': "&quot;"})
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr>'
            f'<w:rFonts w:ascii="{nm}" w:hAnsi="{nm}" w:cs="{nm}" w:eastAsia="{nm}"/>'
            '<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
            '</w:pPr></w:pPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
            '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
            '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
            '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
            '<w:basedOn w:val="Normal"/><w:qFormat/></w:style>'
            '</w:styles>')


_BULLETS = ["\u25cf", "\u25cb", "\u25a0"]


def _numbering_xml(numbered: list[tuple[int, int, int]]) -> str:
    def lvls(bullet: bool) -> str:
        out = []
        for lv in range(9):
            txt = _BULLETS[min(lv, 2)] if bullet else f"%{lv + 1}."
            out.append(f'<w:lvl w:ilvl="{lv}"><w:start w:val="1"/><w:numFmt w:val="{"bullet" if bullet else "decimal"}"/>'
                       f'<w:lvlText w:val="{txt}"/><w:lvlJc w:val="left"/>'
                       f'<w:pPr><w:ind w:left="{(lv + 1) * LIST_INDENT}" w:hanging="{LIST_INDENT}"/></w:pPr></w:lvl>')
        return "".join(out)
    nums = ['<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
    for num_id, lv, start in numbered:
        nums.append(f'<w:num w:numId="{num_id}"><w:abstractNumId w:val="1"/>'
                    f'<w:lvlOverride w:ilvl="{lv}"><w:startOverride w:val="{start}"/></w:lvlOverride></w:num>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:numbering xmlns:w="{W}">'
            f'<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>{lvls(True)}</w:abstractNum>'
            f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>{lvls(False)}</w:abstractNum>'
            + "".join(nums) + "</w:numbering>")


WP_PAGE_W = 64 * 144                    # the WP's text width: 64 columns of 10 cpi, in twips
_BORDER = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'


def _table_xml(rows: list, fonts, colors, deff) -> str:
    """rows: _Para with .row (cells -> paragraphs -> runs), .cellx, .header -> a w:tbl."""
    n = max(1, max(len(r.row) for r in rows))
    cx = rows[0].cellx
    widths = [b - a for a, b in zip([0] + cx, cx)] if len(cx) == n and all(b > a for a, b in zip([0] + cx, cx)) \
        else [WP_PAGE_W // n] * n
    out = ['<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>',
           "".join(_BORDER.format(s) for s in ("top", "left", "bottom", "right", "insideH", "insideV")),
           '</w:tblBorders><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>',
           "".join(f'<w:gridCol w:w="{w}"/>' for w in widths), "</w:tblGrid>"]
    for r in rows:
        out.append("<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if r.header else ""))
        for k in range(n):
            cell = r.row[k] if k < len(r.row) else [[]]
            out.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[k]}" w:type="dxa"/></w:tcPr>')
            for runs in cell or [[]]:
                out.append("<w:p>" + "".join(_run_xml(tuple(pr), t, fonts, colors, deff) for pr, t in runs) + "</w:p>")
            out.append("</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def wp_rtf_to_docx(data: bytes) -> bytes:
    paras, fonts, colors, page, deff = _parse_rtf(data)
    wp = any(p.xwp is not None for p in paras)
    out: list[dict] = []
    numbered: list[tuple[int, int, int]] = []
    num_state: dict[int, tuple[int, int]] = {}        # level -> (numId, next number)
    for p in paras:
        if p.row is not None:
            out.append({"row": p})
            continue
        v = p.xwp
        if wp and v is not None:
            if v & 128 and out and "row" not in out[-1]:
                prev = out[-1]
                txt = "".join(t for _, t in prev["runs"])
                if txt and not txt.endswith(" ") and prev["runs"]:
                    prev["runs"][-1][1] += " "
                prev["runs"].extend(p.runs)
                continue
            al = ("left", "center", "right", "left")[v & 3]
            lst, ind = (v >> 2) & 3, (v >> 4) & 7
            d = {"runs": p.runs, "align": al, "left": ind * LIST_INDENT, "first": 0, "num": None,
                 "pagebb": p.pagebb}
            if lst:
                marker, d["runs"] = _strip_chars(p.runs, 4)
                if lst == 1:
                    d["num"] = (1, ind)
                else:
                    m = re.match(r"\s*(\d+)", marker)
                    n = int(m.group(1)) if m else 1
                    st = num_state.get(ind)
                    if st is None or st[1] != n:
                        nid = 2 + len(numbered)
                        numbered.append((nid, ind, n))
                        st = (nid, n)
                    num_state[ind] = (st[0], n + 1)
                    d["num"] = (st[0], ind)
            out.append(d)
        else:
            out.append({"runs": p.runs, "align": p.align, "left": max(0, p.li), "first": p.fi, "num": None,
                        "pagebb": p.pagebb})
    body = []
    rows: list = []
    for i, d in enumerate(out):
        if "row" in d:
            if rows and d["row"].header:
                body.append(_table_xml(rows, fonts, colors, deff) + "<w:p/>")
                rows = []
            rows.append(d["row"])
            if i + 1 == len(out) or "row" not in out[i + 1]:
                body.append(_table_xml(rows, fonts, colors, deff))
                if i + 1 == len(out):
                    body.append("<w:p/>")
                rows = []
            continue
        ppr = []
        if d["pagebb"]:
            ppr.append("<w:pageBreakBefore/>")
        if d["num"]:
            ppr.append(f'<w:numPr><w:ilvl w:val="{d["num"][1]}"/><w:numId w:val="{d["num"][0]}"/></w:numPr>')
        elif d["left"] or d["first"]:
            fl = d["first"]
            extra = f' w:hanging="{-fl}"' if fl < 0 else (f' w:firstLine="{fl}"' if fl > 0 else "")
            ppr.append(f'<w:ind w:left="{d["left"]}"{extra}/>')
        if d["align"] != "left":
            ppr.append(f'<w:jc w:val="{d["align"]}"/>')
        runs = "".join(_run_xml(tuple(pr), t, fonts, colors, deff) for pr, t in d["runs"])
        body.append("<w:p>" + (f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else "") + runs + "</w:p>")
    if not body:
        body.append("<w:p/>")
    pw, ph = page.get("paperw", 12240), page.get("paperh", 15840)
    sect = (f'<w:sectPr><w:pgSz w:w="{pw}" w:h="{ph}"/><w:pgMar w:top="{page.get("margt", 1440)}" '
            f'w:right="{page.get("margr", 1440)}" w:bottom="{page.get("margb", 1080)}" '
            f'w:left="{page.get("margl", 1440)}" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           f'<w:document xmlns:w="{W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           "<w:body>" + "".join(body) + sect + "</w:body></w:document>")
    return deflated_zip([
        ("[Content_Types].xml", _CT_DOCX.encode()),
        ("_rels/.rels", _RELS_DOCX.encode()),
        ("docProps/app.xml", _APP.encode()),
        ("word/document.xml", doc.encode("utf-8")),
        ("word/styles.xml", _styles_xml(fonts.get(deff, "Courier New")).encode("utf-8")),
        ("word/numbering.xml", _numbering_xml(numbered).encode("utf-8")),
        ("word/_rels/document.xml.rels", _DOC_RELS.encode()),
    ])


# ====================================================================== DOCX -> RTF

_HL_NAMES = {"yellow": (255, 255, 0), "green": (0, 255, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255),
             "blue": (0, 0, 255), "red": (255, 0, 0), "darkBlue": (0, 0, 128), "darkCyan": (0, 128, 128),
             "darkGreen": (0, 128, 0), "darkMagenta": (128, 0, 128), "darkRed": (128, 0, 0),
             "darkYellow": (128, 128, 0), "darkGray": (128, 128, 128), "lightGray": (192, 192, 192),
             "black": (0, 0, 0), "white": (255, 255, 255)}


_GREYS = {0, 1, 11, 12, 15}


def nearest_palette(rgb: tuple[int, int, int]) -> int:
    """Nearest WP palette index ("redmean" distance); clearly coloured input never maps to a grey."""
    r, g, b = rgb
    chroma = max(rgb) - min(rgb) >= 64

    def dist(i):
        pr, pg, pb = WP_PALETTE[i]
        rm = (r + pr) / 2
        return (2 + rm / 256) * (r - pr) ** 2 + 4 * (g - pg) ** 2 + (2 + (255 - rm) / 256) * (b - pb) ** 2
    return min((i for i in range(16) if not (chroma and i in _GREYS)), key=dist)


def _rgb(s: str | None) -> tuple[int, int, int] | None:
    if not s or not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        return None
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _on(el) -> bool:
    return (el.get(_W + "val") or "true").lower() not in ("0", "false", "off", "none")


def _apply_rpr(el, base: dict) -> dict:
    d = dict(base)
    if el is None:
        return d
    for ch in el:
        tag = ch.tag[len(_W):] if ch.tag.startswith(_W) else ""
        if tag in ("b", "i", "vanish"):
            d[tag] = _on(ch)
        elif tag == "u":
            d["u"] = (ch.get(_W + "val") or "single") != "none"
        elif tag == "color":
            v = ch.get(_W + "val")
            d["color"] = (0, 0, 0) if v == "auto" else (_rgb(v) or d.get("color"))
        elif tag == "highlight":
            v = ch.get(_W + "val") or "none"
            d["hl"] = _HL_NAMES.get(v)
        elif tag == "shd":
            f = _rgb(ch.get(_W + "fill"))
            if f is not None:
                d["shd"] = f
        elif tag == "rFonts":
            d["font"] = ch.get(_W + "ascii") or ch.get(_W + "hAnsi") or d.get("font")
        elif tag == "sz":
            v = ch.get(_W + "val") or ""
            if v.isdigit():
                d["sz"] = int(v)
    return d


def wp_size(half_points: int | None) -> int:
    """the X16 WP's sizes: 12, 18, 24 or 36 pt, as \\fs (half points), the nearest to a Word size"""
    hp = half_points or 24
    return 24 if hp < 30 else 36 if hp < 42 else 48 if hp < 60 else 72


def _apply_ppr(el, base: dict) -> dict:
    d = dict(base)
    if el is None:
        return d
    for ch in el:
        tag = ch.tag[len(_W):] if ch.tag.startswith(_W) else ""
        if tag == "jc":
            d["jc"] = ch.get(_W + "val") or "left"
        elif tag == "ind":
            v = ch.get(_W + "left") or ch.get(_W + "start")
            try:
                if v is not None:
                    d["left"] = int(float(v))
            except ValueError:
                pass
        elif tag == "numPr":
            il, ni = ch.find(_W + "ilvl"), ch.find(_W + "numId")
            if ni is not None:
                d["numId"] = ni.get(_W + "val")
            if il is not None:
                d["ilvl"] = il.get(_W + "val")
        elif tag == "pageBreakBefore":
            d["pbb"] = _on(ch)
    return d


class _Styles:
    def __init__(self, root):
        self.st: dict[str, tuple] = {}
        self.rdef: dict = {}
        self.pdef: dict = {}
        self.default_para = None
        if root is None:
            return
        dd = root.find(_W + "docDefaults")
        if dd is not None:
            self.rdef = _apply_rpr(dd.find(f"{_W}rPrDefault/{_W}rPr"), {})
            self.pdef = _apply_ppr(dd.find(f"{_W}pPrDefault/{_W}pPr"), {})
        for s in root.findall(_W + "style"):
            sid = s.get(_W + "styleId") or ""
            bo = s.find(_W + "basedOn")
            self.st[sid] = (bo.get(_W + "val") if bo is not None else None, s.find(_W + "rPr"), s.find(_W + "pPr"))
            if s.get(_W + "type") == "paragraph" and _on_attr(s.get(_W + "default")):
                self.default_para = sid

    def chain(self, sid):
        out, seen = [], set()
        while sid and sid in self.st and sid not in seen and len(out) < 16:
            seen.add(sid)
            out.append(self.st[sid])
            sid = self.st[sid][0]
        return list(reversed(out))

    def para(self, sid) -> tuple[dict, dict]:
        r, p = dict(self.rdef), dict(self.pdef)
        for _, rpr, ppr in self.chain(sid or self.default_para):
            r, p = _apply_rpr(rpr, r), _apply_ppr(ppr, p)
        return r, p

    def run(self, sid, base: dict) -> dict:
        for _, rpr, _ in self.chain(sid):
            base = _apply_rpr(rpr, base)
        return base


def _on_attr(v) -> bool:
    return v is not None and v.lower() in ("1", "true", "on")


class _Numbering:
    def __init__(self, root):
        self.abs: dict[str, dict[int, tuple[str, int]]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.cnt: dict[str, list] = {}
        if root is None:
            return
        for a in root.findall(_W + "abstractNum"):
            lv = {}
            for l in a.findall(_W + "lvl"):
                try:
                    il = int(l.get(_W + "ilvl") or 0)
                    st = l.find(_W + "start")
                    start = int(st.get(_W + "val")) if st is not None else 1
                except (ValueError, TypeError):
                    continue
                nf = l.find(_W + "numFmt")
                lv[il] = ((nf.get(_W + "val") if nf is not None else None) or "decimal", start)
            self.abs[a.get(_W + "abstractNumId") or ""] = lv
        for n in root.findall(_W + "num"):
            an = n.find(_W + "abstractNumId")
            ov = {}
            for o in n.findall(_W + "lvlOverride"):
                so = o.find(_W + "startOverride")
                try:
                    if so is not None:
                        ov[int(o.get(_W + "ilvl") or 0)] = int(so.get(_W + "val"))
                except (ValueError, TypeError):
                    pass
            self.nums[n.get(_W + "numId") or ""] = (an.get(_W + "val") if an is not None else "", ov)

    def marker(self, num_id: str, ilvl: int) -> str | None:
        if num_id in (None, "0") or num_id not in self.nums:
            return None
        aid, ov = self.nums[num_id]
        fmt, start = self.abs.get(aid, {}).get(ilvl, ("bullet", 1))
        start = ov.get(ilvl, start)
        if fmt == "none":
            return ""
        if fmt == "bullet":
            return ("\u25cf", "\u25cb", "\u25a0")[min(ilvl, 2)] + "   "
        c = self.cnt.setdefault(num_id, [None] * 10)
        c[ilvl] = start if c[ilvl] is None else c[ilvl] + 1
        for k in range(ilvl + 1, 10):
            c[k] = None
        n = c[ilvl]
        if fmt in ("lowerLetter", "upperLetter"):
            s = ""
            k = max(1, n)
            while k:
                k, r = divmod(k - 1, 26)
                s = chr(97 + r) + s
            s = s.upper() if fmt == "upperLetter" else s
        elif fmt in ("lowerRoman", "upperRoman"):
            s = _roman(n)
            s = s.lower() if fmt == "lowerRoman" else s
        else:
            s = str(n)
        s += "."
        return s + " " * max(1, 4 - len(s))


def _roman(n: int) -> str:
    if not 0 < n < 4000:
        return str(n)
    out = ""
    for v, r in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
                 (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out += r
            n -= v
    return out


@dataclass
class _OutPara:
    level: int = 0
    align: str = "left"
    pagebb: bool = False
    segs: list = field(default_factory=list)          # (props dict, text)
    cells: list | None = None                         # a table row: one segs list per cell
    header: bool = False


class _DocxReader:
    def __init__(self, styles: _Styles, numbering: _Numbering):
        self.styles, self.num = styles, numbering
        self.paras: list[_OutPara] = []
        self.lossy = False

    # ---- block level
    def block(self, el, depth=0):
        if depth > 32:
            self.lossy = True
            return
        for ch in el:
            tag = ch.tag
            if tag == _W + "p":
                self.para(ch)
            elif tag == _W + "tbl":
                self.table(ch, depth)
            elif tag in (_W + "sdt",):
                c = ch.find(_W + "sdtContent")
                if c is not None:
                    self.block(c, depth + 1)
            elif tag == _W + "customXml":
                self.block(ch, depth + 1)
            elif tag == _W + "altChunk":
                self.lossy = True

    def table(self, tbl, depth):
        # an X16 Write table: up to 8 columns (more are merged into the 8th), a cell's paragraphs
        # joined with spaces; nested tables are flattened into their cell
        if self.paras and self.paras[-1].cells is not None:
            self.paras.append(_OutPara())             # keep two tables apart
        first_row = True
        for tr in tbl.findall(_W + "tr"):
            out = _OutPara(cells=[], header=first_row)
            first_row = False
            for tc in tr.findall(_W + "tc"):
                if tc.find(".//" + _W + "tbl") is not None:
                    self.lossy = True
                sub = _DocxReader(self.styles, self.num)
                for p in tc.iter(_W + "p"):
                    sub.para(p)
                self.lossy |= sub.lossy
                segs: list = []
                for sp in sub.paras:
                    if segs and sp.segs:
                        segs.append(({}, " "))
                    segs.extend(sp.segs)
                if len(out.cells) == 8:
                    self.lossy = True
                    out.cells[7].append(({}, " "))
                    out.cells[7].extend(segs)
                else:
                    out.cells.append(segs)
            if not out.cells:
                out.cells.append([])
            self.paras.append(out)

    def para(self, p):
        ppr = p.find(_W + "pPr")
        sid = None
        if ppr is not None:
            ps = ppr.find(_W + "pStyle")
            sid = ps.get(_W + "val") if ps is not None else None
        rbase, pp = self.styles.para(sid)
        pp = _apply_ppr(ppr, pp)
        jc = pp.get("jc", "left")
        align = "center" if jc == "center" else ("right" if jc in ("right", "end") else "left")
        out = _OutPara(align=align, pagebb=bool(pp.get("pbb")))
        marker = None
        try:
            ilvl = int(pp.get("ilvl") or 0)
        except ValueError:
            ilvl = 0
        ilvl = max(0, min(ilvl, 8))
        if pp.get("numId"):
            marker = self.num.marker(pp["numId"], ilvl)
        if marker is not None:
            out.level = min(ilvl, 6)
            if marker:
                out.segs.append(({}, marker))
        else:
            out.level = max(0, min(6, round(pp.get("left", 0) / LIST_INDENT)))
        self.cur = out
        self.paras.append(out)
        self.fld: list[str] = []
        self.inline(p, rbase, 0)

    def _break(self, page: bool):
        cur = self.cur
        nxt = _OutPara(level=cur.level, align=cur.align, pagebb=page)
        self.paras.append(nxt)
        self.cur = nxt

    def inline(self, el, rbase, depth):
        if depth > 32:
            self.lossy = True
            return
        for ch in el:
            tag = ch.tag
            if tag == _W + "r":
                self.run(ch, rbase)
            elif tag in (_W + "hyperlink", _W + "ins", _W + "moveTo", _W + "smartTag", _W + "customXml",
                         _W + "fldSimple", _W + "bdo", _W + "dir"):
                self.inline(ch, rbase, depth + 1)
            elif tag == _W + "sdt":
                c = ch.find(_W + "sdtContent")
                if c is not None:
                    self.inline(c, rbase, depth + 1)
            elif tag.endswith("}oMath") or tag.endswith("}oMathPara"):
                self.lossy = True
                txt = "".join(t.text or "" for t in ch.iter() if t.tag.endswith("}t"))
                if txt:
                    self.cur.segs.append((rbase, txt))

    def run(self, r, rbase):
        rpr = r.find(_W + "rPr")
        props = rbase
        if rpr is not None:
            rs = rpr.find(_W + "rStyle")
            if rs is not None:
                props = self.styles.run(rs.get(_W + "val"), props)
            props = _apply_rpr(rpr, props)
        if props.get("vanish"):
            return
        for ch in r:
            tag = ch.tag
            if tag == _W + "fldChar":
                t = ch.get(_W + "fldCharType")
                if t == "begin":
                    self.fld.append("instr")
                elif t == "separate" and self.fld:
                    self.fld[-1] = "result"
                elif t == "end" and self.fld:
                    self.fld.pop()
                continue
            if any(s == "instr" for s in self.fld):
                continue
            if tag == _W + "t":
                if ch.text:
                    self.cur.segs.append((props, ch.text))
            elif tag in (_W + "tab", _W + "ptab"):
                self.cur.segs.append((props, "\t"))
            elif tag == _W + "br":
                self._break(ch.get(_W + "type") == "page")
            elif tag == _W + "cr":
                self._break(False)
            elif tag == _W + "noBreakHyphen":
                self.cur.segs.append((props, "-"))
            elif tag == _W + "sym":
                self.cur.segs.append((props, "?"))
                self.lossy = True
            elif tag in (_W + "drawing", _W + "pict", _W + "object", _W + "footnoteReference",
                         _W + "endnoteReference") or tag.endswith("}AlternateContent"):
                self.lossy = True


_HEADER_WP = (
    "{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033\r\n"
    "{\\fonttbl{\\f0\\fmodern\\fcharset0 Courier New;}{\\f1\\fmodern\\fcharset0 Lucida Console;}"
    "{\\f2\\fmodern\\fcharset0 Andale Mono;}}\r\n"
    "{\\colortbl;" + "".join(f"\\red{r}\\green{g}\\blue{b};" for r, g, b in WP_PALETTE) + "}\r\n"
    "\\paperw12240\\paperh15840\\margl1440\\margr1440\\margt1440\\margb1080\\viewkind1\r\n")

_BULLET_RTF = {"\u25cf": "\\u9679?", "\u25cb": "\\u9675?", "\u25a0": "\\u9632?"}


def _font_idx(name: str | None) -> int:
    n = (name or "").casefold()
    if "lucida console" in n:
        return 1
    if "andale" in n:
        return 2
    return 0


def _split_long(paras: list[_OutPara]) -> tuple[list[_OutPara], bool]:
    out, lossy = [], False
    for p in paras:
        if p.cells is not None:
            out.append(p)
            continue
        while sum(len(t) for _, t in p.segs) > WP_PARA_MAX:
            lossy = True
            text = "".join(t for _, t in p.segs)
            cut = text.rfind(" ", 0, WP_PARA_MAX)
            cut = cut + 1 if cut > WP_PARA_MAX // 2 else WP_PARA_MAX
            head, rest, n = [], [], cut
            for pr, t in p.segs:
                if n >= len(t):
                    head.append((pr, t))
                    n -= len(t)
                elif n > 0:
                    head.append((pr, t[:n]))
                    rest.append((pr, t[n:]))
                    n = 0
                else:
                    rest.append((pr, t))
            out.append(_OutPara(p.level, p.align, p.pagebb, head))
            p = _OutPara(p.level, p.align, False, rest)
        out.append(p)
    return out, lossy


def _segs_rtf(segs: list, bullets: bool) -> tuple[bytes, bool]:
    """runs (props dict, text) -> RTF text with \\b \\i \\ul \\fN \\cfN \\highlightN, as after \\plain."""
    out, lossy = bytearray(), False
    cur = (False, False, False, 0, 0, 1, 24)        # b i u font fg bg (palette indices) fs, as after \plain\fs24
    for k, (pr, text) in enumerate(segs):
        if bullets and k == 0 and pr == {} and text[:1] in _BULLET_RTF:
            out += _BULLET_RTF[text[0]].encode() + text[1:].encode("ascii")
            continue
        fg = nearest_palette(pr["color"]) if pr.get("color") else 0
        hl = pr.get("hl") or pr.get("shd")
        bg = nearest_palette(hl) if hl else 1
        want = (bool(pr.get("b")), bool(pr.get("i")), bool(pr.get("u")), _font_idx(pr.get("font")), fg, bg,
                wp_size(pr.get("sz")))
        if want != cur:
            cw = b""
            if want[0] != cur[0]:
                cw += b"\\b" if want[0] else b"\\b0"
            if want[1] != cur[1]:
                cw += b"\\i" if want[1] else b"\\i0"
            if want[2] != cur[2]:
                cw += b"\\ul" if want[2] else b"\\ulnone"
            if want[3] != cur[3]:
                cw += b"\\f%d" % want[3]
            if want[4] != cur[4]:
                cw += b"\\cf%d" % (want[4] + 1)
            if want[5] != cur[5]:
                cw += b"\\highlight%d" % (0 if want[5] == 1 else want[5] + 1)
            if want[6] != cur[6]:
                cw += b"\\fs%d" % want[6]
            out += cw + b" "
            cur = want
        s = text.translate(_FOLD)
        for ch in s:
            if ch == "\t":
                out += b"\\tab "
                continue
            if ord(ch) < 32:
                continue
            try:
                c = ch.encode(WP_CHARSET)[0]
            except UnicodeEncodeError:
                c, lossy = 0x3F, True
            if c in (0x5C, 0x7B, 0x7D):
                out += b"\\" + bytes([c])
            elif c >= 128:
                out += b"\\'%02x" % c
            else:
                out.append(c)
    return bytes(out), lossy


def docx_to_rtf(data: bytes) -> tuple[bytes, bool]:
    zf = open_zip(data)
    doc_path = office_document(zf, "word/document.xml")
    b = read_part(zf, doc_path, PART_CAP)
    if b is None:
        raise ConvError("not a Word document (no document.xml)")
    root = parse_xml(b, "document")
    body = root.find(_W + "body")
    if body is None:
        raise ConvError("not a Word document (no body)")
    d_dir, d_name = doc_path.rsplit("/", 1) if "/" in doc_path else ("", doc_path)
    rels = rels_targets(zf, f"{d_dir}/_rels/{d_name}.rels" if d_dir else f"_rels/{d_name}.rels", d_dir)
    styles_root = num_root = None
    for typ, tgt in rels.values():
        if typ == "styles":
            sb = read_part(zf, tgt)
            styles_root = parse_xml(sb, "styles") if sb else None
        elif typ == "numbering":
            nb = read_part(zf, tgt)
            num_root = parse_xml(nb, "numbering") if nb else None
    rd = _DocxReader(_Styles(styles_root), _Numbering(num_root))
    rd.block(body)
    paras, lossy = _split_long(rd.paras)
    lossy |= rd.lossy
    out = bytearray(_HEADER_WP.encode("ascii"))
    for p in paras:
        if p.cells is not None:
            n = len(p.cells)
            cw = 63 // n                                # the WP's cell: cw - 1 characters + a border
            out += b"\\trowd\\trgaph72\\trleft0" + (b"\\trhdr" if p.header else b"")
            for k in range(n):
                out += b"\\clbrdrt\\brdrs\\brdrw10\\clbrdrl\\brdrs\\brdrw10\\clbrdrb\\brdrs\\brdrw10" \
                       b"\\clbrdrr\\brdrs\\brdrw10\\cellx%d" % ((k + 1) * cw * 144)
            out += b"\r\n"
            for segs in p.cells:
                if sum(len(t) for _, t in segs) > cw - 1:
                    lossy = True
                t, ls = _segs_rtf(segs, False)
                out += b"\\pard\\plain\\intbl\\sb0\\sa0\\sl-240\\slmult0\\ql\\fs24 " + t + b"\\cell\r\n"
                lossy |= ls
            out += b"\\row\r\n"
            continue
        out += b"\\pard\\plain" + {"center": b"\\qc", "right": b"\\qr"}.get(p.align, b"\\ql")
        out += b"\\li%d" % (p.level * LIST_INDENT)
        if p.pagebb:
            out += b"\\pagebb"
        out += b"\\fs24 "
        t, ls = _segs_rtf(p.segs, True)
        out += t
        lossy |= ls
        out += b"\\par\r\n"
    out += b"}\r\n"
    return bytes(out), lossy
