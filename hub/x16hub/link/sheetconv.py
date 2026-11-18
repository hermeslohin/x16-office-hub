"""X16 Sheets <-> XLSX / CSV conversion (docs channel, conv=2).

The X16 side speaks the "simple XLSX" of commanderx16sheets/SHEETFMT.md.
write_simple() is the reference writer for that format: the X16 produces the
same entries in the same order with the same XML layout:

* entries (all stored, DOS time 1980-01-01 00:00, no extras):
  [Content_Types].xml, _rels/.rels, xl/workbook.xml, xl/_rels/workbook.xml.rels,
  xl/styles.xml, xl/worksheets/sheet1.xml -- all but the sheet are the constants below.
* sheet1.xml: XML declaration line, <worksheet ...> line, optional <cols> line
  (only when some column has a width), "<sheetData>" line, one line per <row>,
  then "</sheetData></worksheet>". Lines end in "\\n". Attribute order r, s, t;
  s="0" is omitted. Text: <is><t>..</t></is>, with xml:space="preserve" on <t>
  only when the text starts or ends with a space. Formula cells: t="str"
  (cached text) / t="e" (cached error) / no t (cached number or none), <f> then
  optional <v>. Numbers: integers without ".0", else Python repr (E for the exponent).
  Widths are integers 3..40; consecutive columns with the same width share one <col>.

Public API:
  read_sheet(data, limits=True) -> (Sheet, lossy)    any .xlsx, first sheet only
  write_simple(sheet) -> bytes                       stored simple XLSX
  xlsx_to_simple(data) -> (bytes, lossy)
  simple_to_xlsx(data) -> bytes                      validate + re-write deflated
  csv_to_simple(data) -> (bytes, lossy)
  simple_to_csv(data) -> bytes                       UTF-8, formula cells -> cached value
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field

from .ooxml import (PART_CAP, ConvError, deflated_zip, esc_ascii, office_document, open_zip, parse_xml,
                    read_part, rels_targets, stored_zip)
from .rtfconv import _FOLD, WP_CHARSET, decode_utf8

__all__ = ["Cell", "Sheet", "ConvError", "read_sheet", "write_simple", "xlsx_to_simple", "simple_to_xlsx",
           "csv_to_simple", "simple_to_csv", "STYLES_XML"]

MAX_COLS, MAX_ROWS, MAX_CELLS, MAX_TEXT = 256, 9999, 6000, 50
MAX_FORMULA = 46          # X16 Sheets keeps 6 bytes of each formula record for its recalculation box
HARD_CELLS = 200_000            # absolute cap when limits are off (X16 upload validation)
MAX_SST = 1_000_000
MIN_W, MAX_W = 3, 40

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_N = "{%s}" % NS
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# ------------------------------------------------------------------ model


@dataclass
class Cell:
    """t: 'n' number, 's' text, 'e' error (formula cached value only).
    Constant cell: f is None, v is float ('n') or str ('s').
    Formula cell: f is the formula (no '='), v the cached value or None, t its type."""
    v: float | str | None
    t: str = "n"
    f: str | None = None
    s: int = 0


@dataclass
class Sheet:
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)   # (row, col), 1-based
    widths: dict[int, int] = field(default_factory=dict)              # col -> width (chars)


def col_name(c: int) -> str:
    s = ""
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def col_num(s: str) -> int:
    n = 0
    for ch in s.upper():
        n = n * 26 + ord(ch) - 64
    return n


_CELLREF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")


def parse_ref(r: str) -> tuple[int, int] | None:
    m = _CELLREF.match(r or "")
    if not m:
        return None
    return int(m.group(2)), col_num(m.group(1))


def num_text(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x).replace("e", "E")


# ------------------------------------------------------------------ constant parts

_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

CONTENT_TYPES_XML = (
    _DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>')

ROOT_RELS_XML = (
    _DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="xl/workbook.xml"/></Relationships>')

WORKBOOK_XML = (
    _DECL + f'<workbook xmlns="{NS}" xmlns:r="{NS_R}">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
    '<calcPr fullCalcOnLoad="1"/></workbook>')

WORKBOOK_RELS_XML = (
    _DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'styles" Target="styles.xml"/></Relationships>')

_FMT_IDS = (0, 2, 10, 164)
_ALIGNS = (None, "left", "center", "right")


def _xf(s: int) -> str:
    bold, align, fmt = s & 1, (s >> 1) & 3, s >> 3
    nf = _FMT_IDS[fmt]
    a = f'<xf numFmtId="{nf}" fontId="{bold}" fillId="0" borderId="0" xfId="0"'
    if nf:
        a += ' applyNumberFormat="1"'
    if bold:
        a += ' applyFont="1"'
    if align:
        return a + f' applyAlignment="1"><alignment horizontal="{_ALIGNS[align]}"/></xf>'
    return a + "/>"


STYLES_XML = (
    _DECL + f'<styleSheet xmlns="{NS}">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="32">' + "".join(_xf(s) for s in range(32)) + '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')

SHEET_HEAD = _DECL + f'<worksheet xmlns="{NS}">\n'

# ------------------------------------------------------------------ writer


def _t(s: str) -> str:
    sp = ' xml:space="preserve"' if s[:1] == " " or s[-1:] == " " else ""
    return f"<t{sp}>{esc_ascii(s)}</t>"


def _cell_xml(r: int, c: int, cell: Cell) -> str:
    a = f'<c r="{col_name(c)}{r}"'
    if cell.s:
        a += f' s="{cell.s}"'
    if cell.f is None:
        if cell.t == "s":
            return a + f' t="inlineStr"><is>{_t(str(cell.v))}</is></c>'
        return a + f"><v>{num_text(float(cell.v))}</v></c>"
    if cell.v is None:
        return a + f"><f>{esc_ascii(cell.f)}</f></c>"
    if cell.t == "n":
        return a + f"><f>{esc_ascii(cell.f)}</f><v>{num_text(float(cell.v))}</v></c>"
    tt = "e" if cell.t == "e" else "str"
    return a + f' t="{tt}"><f>{esc_ascii(cell.f)}</f><v>{esc_ascii(str(cell.v))}</v></c>'


def sheet_xml(sh: Sheet) -> str:
    out = [SHEET_HEAD]
    if sh.widths:
        cols = []
        run = None
        for c in sorted(sh.widths):
            w = sh.widths[c]
            if run and run[1] == c - 1 and run[2] == w:
                run[1] = c
            else:
                if run:
                    cols.append(run)
                run = [c, c, w]
        cols.append(run)
        out.append("<cols>" + "".join(f'<col min="{a}" max="{b}" width="{w}" customWidth="1"/>' for a, b, w in cols)
                   + "</cols>\n")
    out.append("<sheetData>\n")
    rows: dict[int, list[int]] = {}
    for r, c in sh.cells:
        rows.setdefault(r, []).append(c)
    for r in sorted(rows):
        out.append(f'<row r="{r}">' + "".join(_cell_xml(r, c, sh.cells[(r, c)]) for c in sorted(rows[r]))
                   + "</row>\n")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def _entries(sh: Sheet) -> list[tuple[str, bytes]]:
    return [("[Content_Types].xml", CONTENT_TYPES_XML.encode()), ("_rels/.rels", ROOT_RELS_XML.encode()),
            ("xl/workbook.xml", WORKBOOK_XML.encode()), ("xl/_rels/workbook.xml.rels", WORKBOOK_RELS_XML.encode()),
            ("xl/styles.xml", STYLES_XML.encode()), ("xl/worksheets/sheet1.xml", sheet_xml(sh).encode("ascii"))]


def write_simple(sh: Sheet) -> bytes:
    """The simple XLSX exactly as the X16 writes it (stored entries)."""
    return stored_zip(_entries(sh))


# ------------------------------------------------------------------ text normalisation

_XESC = re.compile(r"_x([0-9A-Fa-f]{4})_")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _norm_text(s: str, limits: bool, cap: int = MAX_TEXT) -> tuple[str, bool]:
    """-> ISO-8859-15 representable text ('?' for the rest), control chars -> space,
    truncated to `cap` bytes when limits are on."""
    s = _CTRL.sub(" ", s.translate(_FOLD))
    b = s.encode(WP_CHARSET, "replace")
    lossy = b.decode(WP_CHARSET) != s
    if limits and len(b) > cap:
        b, lossy = b[:cap], True
    return b.decode(WP_CHARSET), lossy


# ------------------------------------------------------------------ styles

_BUILTIN_FMT = {0: 0, 1: 0, 2: 1, 3: 0, 4: 1, 5: 3, 6: 3, 7: 3, 8: 3, 9: 2, 10: 2, 11: 0, 12: 0, 13: 0,
                37: 0, 38: 0, 39: 1, 40: 1, 41: 0, 42: 3, 43: 1, 44: 3, 48: 0, 49: 0}
_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def classify_numfmt(code: str) -> tuple[int, bool]:
    """Format code -> (fmt class 0..3, lossy). lossy = date/time shown as a plain number."""
    sec, q = "", False
    for ch in code:                        # first section only
        if ch == '"':
            q = not q
        elif ch == ";" and not q:
            break
        sec += ch
    if sec.strip().lower() in ("general", ""):
        return 0, False
    cur = any(ch in qs for qs in re.findall(r'"([^"]*)"', sec) for ch in "$€£¥")
    for br in re.findall(r"\[([^\]]*)\]", sec):
        if br.startswith("$") and len(br) > 1 and br[1] != "-":
            cur = True
    bare = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.|[_*].', "", sec)
    if any(ch in bare for ch in "$€£¥"):
        cur = True
    low = bare.lower()
    if "%" in bare:
        return 2, False
    if cur:
        return 3, False
    if "e+" in low or "e-" in low:
        return 0, False
    if re.search(r"[ymdhs]", low):
        return 0, True
    if re.search(r"\.[0#?]", bare):
        return 1, False
    return 0, False


def _true(el) -> bool:
    return el is not None and (el.get("val") or "1").lower() not in ("0", "false", "off")


def _style_map(root) -> list[tuple[int, bool]]:
    """cellXfs index -> (simple style index, lossy)."""
    if root is None:
        return []
    custom = {}
    nfs = root.find(_N + "numFmts")
    if nfs is not None:
        for nf in nfs.findall(_N + "numFmt"):
            try:
                custom[int(nf.get("numFmtId", ""))] = nf.get("formatCode") or ""
            except ValueError:
                pass
    bold = []
    fonts = root.find(_N + "fonts")
    if fonts is not None:
        bold = [_true(f.find(_N + "b")) for f in fonts.findall(_N + "font")]
    out = []
    xfs = root.find(_N + "cellXfs")
    for xf in (xfs.findall(_N + "xf") if xfs is not None else []):
        try:
            nid = int(xf.get("numFmtId") or 0)
            fid = int(xf.get("fontId") or 0)
        except ValueError:
            nid = fid = 0
        if nid in custom:
            fmt, lossy = classify_numfmt(custom[nid])
        elif nid in _DATE_IDS:
            fmt, lossy = 0, True
        else:
            fmt, lossy = _BUILTIN_FMT.get(nid, 0), False
        al = xf.find(_N + "alignment")
        h = (al.get("horizontal") if al is not None else None) or "general"
        align = {"left": 1, "center": 2, "centerContinuous": 2, "right": 3}.get(h, 0)
        b = 1 if 0 <= fid < len(bold) and bold[fid] else 0
        out.append((b + 2 * align + 8 * fmt, lossy))
    return out


# ------------------------------------------------------------------ formulas

_FTOK = re.compile(r"""("(?:[^"]|"")*")"""                         # 1 string literal
                   r"""|('(?:[^']|'')+'!|(?<![\w.])[A-Za-z_][\w.]*!)"""  # 2 sheet prefix
                   r"""|(?<![\w.$])(\$?)([A-Za-z]{1,3})(\$?)(\d{1,7})(?![\w.(!])""")  # 3-6 A1 ref
_XLFN = re.compile(r"_xl(?:fn|ws)\.", re.I)


def translate_formula(f: str, first_sheet: str, dr: int = 0, dc: int = 0) -> str:
    """Strip '=', _xlfn. prefixes and references' sheet names that point at the first
    sheet; shift relative A1 refs by (dr, dc) (shared-formula followers).
    Whole-row/whole-column ranges (1:1, A:A) are not shifted."""
    f = f.strip()
    if f.startswith("="):
        f = f[1:]
    first = first_sheet.casefold()

    def sub(m):
        if m.group(1):
            return m.group(1)
        if m.group(2):
            name = m.group(2)[:-1]
            if name.startswith("'"):
                name = name[1:-1].replace("''", "'")
            return "" if name.casefold() == first else m.group(2)
        ca, col, ra, row = m.group(3), m.group(4), m.group(5), m.group(6)
        c, r = col_num(col), int(row)
        if c > 16384 or r > 1048576:
            return m.group(0)
        if not ca:
            c += dc
        if not ra:
            r += dr
        if c < 1 or r < 1:
            return "#REF!"
        return f"{ca}{col_name(c)}{ra}{r}"
    f = _FTOK.sub(sub, f)
    out = []                               # _xlfn. outside string literals only
    for part in re.split(r'("(?:[^"]|"")*")', f):
        out.append(part if part.startswith('"') else _XLFN.sub("", part))
    return "".join(out)


# ------------------------------------------------------------------ reader


def _text_of(el) -> str:
    """Concatenated <t> text of an <is>/<si>, skipping phonetic runs."""
    parts = []
    for ch in el:
        tag = ch.tag
        if tag == _N + "t":
            parts.append(ch.text or "")
        elif tag == _N + "r":
            t = ch.find(_N + "t")
            if t is not None:
                parts.append(t.text or "")
    return _XESC.sub(lambda m: chr(int(m.group(1), 16)), "".join(parts))


def _num(s: str | None) -> float | None:
    try:
        x = float((s or "").strip())
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def read_sheet(data: bytes, *, limits: bool = True) -> tuple[Sheet, bool]:
    """First worksheet of any .xlsx -> (Sheet, lossy). limits=True applies the X16
    limits (SHEETFMT.md) with truncation; limits=False only applies safety caps."""
    zf = open_zip(data)
    wb_path = office_document(zf, "xl/workbook.xml")
    wb_dir = wb_path.rsplit("/", 1)[0] if "/" in wb_path else ""
    wb = read_part(zf, wb_path)
    if wb is None:
        raise ConvError("not a spreadsheet (no workbook)")
    wbr = parse_xml(wb, "workbook")
    rels = rels_targets(zf, f"{wb_dir}/_rels/{wb_path.rsplit('/', 1)[-1]}.rels" if wb_dir
                        else f"_rels/{wb_path}.rels", wb_dir)
    sheets = wbr.find(_N + "sheets")
    first = sheets.find(_N + "sheet") if sheets is not None else None
    if first is None:
        raise ConvError("workbook has no sheets")
    lossy = len(sheets.findall(_N + "sheet")) > 1
    sheet_name = first.get("name") or "Sheet1"
    rid = first.get(f"{{{NS_R}}}id")
    sheet_path = rels.get(rid, ("", ""))[1] if rid else ""
    if not sheet_path:
        sheet_path = "xl/worksheets/sheet1.xml"
    sst: list[str] = []
    styles = None
    for typ, tgt in rels.values():
        if typ == "sharedStrings":
            b = read_part(zf, tgt, PART_CAP)
            if b is not None:
                for si in parse_xml(b, "shared strings").iter(_N + "si"):
                    sst.append(_text_of(si))
                    if len(sst) > MAX_SST:
                        raise ConvError("too many shared strings")
        elif typ == "styles":
            b = read_part(zf, tgt)
            if b is not None:
                styles = parse_xml(b, "styles")
    smap = _style_map(styles)
    b = read_part(zf, sheet_path, PART_CAP)
    if b is None:
        raise ConvError("first worksheet is missing")
    root = parse_xml(b, "worksheet")
    if not root.tag.endswith("}worksheet"):
        raise ConvError("first sheet is not a worksheet (chart sheet?)")
    sh = Sheet()

    def textval(s: str) -> str:
        nonlocal lossy
        t, l2 = _norm_text(s, limits)
        lossy |= l2
        return t

    cols = root.find(_N + "cols")
    if cols is not None:
        for col in cols.findall(_N + "col"):
            try:
                a, z, w = int(col.get("min", "0")), int(col.get("max", "0")), float(col.get("width", "nan"))
            except ValueError:
                continue
            if not math.isfinite(w) or a < 1 or z < a:
                continue
            w = max(MIN_W, min(MAX_W, int(round(w))))
            for c in range(a, min(z, MAX_COLS) + 1):
                sh.widths[c] = w
    shared: dict[str, tuple[str, int, int]] = {}
    sd = root.find(_N + "sheetData")
    cap = MAX_CELLS if limits else HARD_CELLS
    rowno = 0
    for row in (sd if sd is not None else []):
        if row.tag != _N + "row":
            continue
        try:
            rowno = int(row.get("r")) if row.get("r") else rowno + 1
        except ValueError:
            rowno += 1
        colno = 0
        for c in row:
            if c.tag != _N + "c":
                continue
            pos = parse_ref(c.get("r", ""))
            r, colno = (pos[0], pos[1]) if pos else (rowno, colno + 1)
            t = c.get("t") or "n"
            fe, ve = c.find(_N + "f"), c.find(_N + "v")
            raw_v = ve.text if ve is not None else None
            try:
                si = int(c.get("s") or 0)
            except ValueError:
                si = 0
            st, sl = smap[si] if 0 <= si < len(smap) else (0, False)
            # ---- value
            if t == "s":
                try:
                    v, vt = sst[int(raw_v or "")], "s"
                except (ValueError, IndexError):
                    v, vt = None, "n"
            elif t == "inlineStr":
                ise = c.find(_N + "is")
                v, vt = (_text_of(ise) if ise is not None else raw_v), "s"
            elif t == "str":
                v, vt = raw_v, "s"
            elif t == "b":
                v, vt = (1.0 if (raw_v or "").strip() in ("1", "true") else 0.0) if raw_v is not None else None, "n"
            elif t == "e":
                v, vt = raw_v, "e"
            elif t == "d":
                v, vt = raw_v, "s"
                lossy = True
            else:
                v, vt = _num(raw_v), "n"
                if v is None and raw_v and raw_v.strip():
                    v, vt = raw_v, "s"
            # ---- formula
            f = None
            if fe is not None:
                ft = fe.get("t") or "normal"
                if ft == "shared" and fe.get("si") is not None:
                    key = fe.get("si")
                    if fe.text:
                        shared[key] = (fe.text, r, colno)
                        f = translate_formula(fe.text, sheet_name)
                    elif key in shared:
                        mt, mr, mc = shared[key]
                        f = translate_formula(mt, sheet_name, r - mr, colno - mc)
                    else:
                        lossy = True
                elif ft == "dataTable":
                    lossy = True
                elif fe.text:
                    f = translate_formula(fe.text, sheet_name)
                    if ft == "array" and fe.get("ref") not in (None, c.get("r")):
                        lossy = True
            if f is None and v is None:
                continue                                  # empty (maybe styled) cell
            if (limits and (r > MAX_ROWS or colno > MAX_COLS)) or r < 1 or colno < 1:
                lossy = True
                continue
            if r > 1048576 or colno > 16384:
                raise ConvError("cell reference out of range")
            if len(sh.cells) >= cap:
                if not limits:
                    raise ConvError("too many cells")
                lossy = True
                continue
            if f is not None:
                ff, fl = _norm_text(f, False)
                if fl or (limits and len(ff.encode(WP_CHARSET)) > MAX_FORMULA):
                    f = None                               # a cut formula is worse than its value
                    lossy = True
                    if v is None:
                        continue
                else:
                    f = ff
            if vt in ("s", "e") and v is not None:
                v = textval(v)
                if f is None and vt == "e":
                    vt = "s"                               # constant error -> shown as text
            lossy |= sl
            sh.cells[(r, colno)] = Cell(v, vt, f, st)
    return sh, lossy


# ------------------------------------------------------------------ conversions


def xlsx_to_simple(data: bytes) -> tuple[bytes, bool]:
    sh, lossy = read_sheet(data, limits=True)
    return write_simple(sh), lossy


def simple_to_xlsx(data: bytes) -> bytes:
    """Validate an X16-written simple XLSX and re-pack it deflated for ownCloud."""
    zf = open_zip(data)
    if read_part(zf, "xl/worksheets/sheet1.xml", PART_CAP) is None:
        raise ConvError("not an X16 sheet (xl/worksheets/sheet1.xml missing)")
    sh, _ = read_sheet(data, limits=False)
    return deflated_zip(_entries(sh))


_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_CUR_RE = re.compile(r"^(-?)\$(\d{1,3}(,\d{3})*|\d+)(\.\d+)?$")
_PCT_RE = re.compile(r"^([+-]?(\d+\.?\d*|\.\d+))%$")


def _csv_value(s: str) -> tuple[float | None, int]:
    """CSV field -> (number or None, simple fmt class)."""
    t = s.strip()
    if _NUM_RE.match(t):
        if len(t) > 1 and t[0] == "0" and t[1].isdigit():
            return None, 0                         # 007, zip codes: keep as text
        return _num(t), 0
    m = _PCT_RE.match(t)
    if m:
        x = _num(m.group(1))
        return (x / 100 if x is not None else None), 2
    m = _CUR_RE.match(t)
    if m:
        x = _num(m.group(2).replace(",", "") + (m.group(4) or ""))
        return (-x if m.group(1) and x is not None else x), 3
    return None, 0


def csv_to_simple(data: bytes) -> tuple[bytes, bool]:
    text = decode_utf8(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    sh = Sheet()
    lossy = False
    maxw: dict[int, int] = {}
    try:
        rows = csv.reader(io.StringIO(text, newline=""), dialect)
        for r, rec in enumerate(rows, 1):
            if r > MAX_ROWS:
                lossy = True
                break
            for c, fld in enumerate(rec, 1):
                if fld == "":
                    continue
                if c > MAX_COLS or len(sh.cells) >= MAX_CELLS:
                    lossy = True
                    continue
                x, fmt = _csv_value(fld)
                if x is not None:
                    sh.cells[(r, c)] = Cell(x, "n", None, 8 * fmt)
                    ln = len(fld.strip())
                else:
                    t, l2 = _norm_text(fld, True)
                    lossy |= l2
                    sh.cells[(r, c)] = Cell(t, "s")
                    ln = len(t)
                maxw[c] = max(maxw.get(c, 0), ln)
    except csv.Error:
        raise ConvError("CSV could not be parsed") from None
    for c, w in maxw.items():
        if w + 1 > 10:
            sh.widths[c] = min(MAX_W, w + 1)
    return write_simple(sh), lossy


def simple_to_csv(data: bytes) -> bytes:
    sh, _ = read_sheet(data, limits=False)
    rows: dict[int, dict[int, str]] = {}
    for (r, c), cell in sh.cells.items():
        if cell.v is None:
            s = ""
        elif cell.t == "n":
            s = num_text(float(cell.v))
        else:
            s = str(cell.v)
        rows.setdefault(r, {})[c] = s
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    for r in range(1, (max(rows) if rows else 0) + 1):
        cs = rows.get(r, {})
        w.writerow([cs.get(c, "") for c in range(1, (max(cs) if cs else 0) + 1)])
    return buf.getvalue().encode("utf-8")
