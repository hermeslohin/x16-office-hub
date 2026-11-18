"""Unit tests for the OOXML converters: sheetconv (X16 Sheets <-> xlsx/csv) and docxconv (X16 Write <-> docx)."""
import io
import re
import zipfile

import pytest

from x16hub.link import docxconv as D
from x16hub.link import sheetconv as S
from x16hub.link.ooxml import ConvError
from x16hub.link.rtfconv import WP_CHARSET, rtf_to_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = S.NS


def zipped(entries, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


# ====================================================================== simple format


def sample_sheet():
    sh = S.Sheet()
    sh.cells[(1, 1)] = S.Cell("Item", "s", None, 1)
    sh.cells[(1, 2)] = S.Cell(12.5)
    sh.cells[(3, 2)] = S.Cell(30.0, "n", "SUM(B1:B2)", 8)
    sh.cells[(3, 3)] = S.Cell("#DIV/0!", "e", "1/0")
    sh.cells[(4, 1)] = S.Cell(" Café <&> €", "s", None, 2 * 3 + 8 * 3)
    sh.cells[(4, 2)] = S.Cell("ok", "s", 'IF(B1>1,"ok","no")')
    sh.cells[(5, 3)] = S.Cell(None, "n", "B3*2")
    sh.widths = {2: 14, 3: 14, 5: 8}
    return sh


def test_simple_writer_matches_sheetfmt_example():
    sh = S.Sheet()
    sh.cells[(1, 1)] = S.Cell("Item", "s", None, 1)
    sh.cells[(1, 2)] = S.Cell(12.5)
    sh.cells[(3, 2)] = S.Cell(30.0, "n", "SUM(B1:B2)", 8)
    sh.cells[(3, 3)] = S.Cell("#DIV/0!", "e", "1/0")
    sh.widths = {2: 14}
    data = S.write_simple(sh)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist() == ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                            "xl/styles.xml", "xl/worksheets/sheet1.xml"]
    for i in z.infolist():
        assert i.compress_type == zipfile.ZIP_STORED and i.flag_bits == 0 and i.extra == b"" and not i.comment
    assert z.read("xl/worksheets/sheet1.xml").decode() == (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n'
        '<cols><col min="2" max="2" width="14" customWidth="1"/></cols>\n'
        '<sheetData>\n'
        '<row r="1"><c r="A1" s="1" t="inlineStr"><is><t>Item</t></is></c><c r="B1"><v>12.5</v></c></row>\n'
        '<row r="3"><c r="B3" s="8"><f>SUM(B1:B2)</f><v>30</v></c><c r="C3" t="e"><f>1/0</f><v>#DIV/0!</v></c></row>\n'
        '</sheetData></worksheet>')
    assert S.write_simple(sh) == data                       # deterministic
    st = z.read("xl/styles.xml").decode()
    assert st.count("<xf ") == 33 and '<cellXfs count="32">' in st and 'numFmtId="164"' in st
    xfs = re.findall(r"<xf [^>]*?(?:/>|>.*?</xf>)", st.split("<cellXfs")[1])
    assert len(xfs) == 32
    assert 'fontId="1"' in xfs[1] and 'horizontal="center"' in xfs[4] and "alignment" not in xfs[8]
    assert 'numFmtId="2"' in xfs[8] and 'numFmtId="10"' in xfs[16] and 'numFmtId="164"' in xfs[31]


def test_simple_roundtrip_and_ascii_only():
    sh = sample_sheet()
    data = S.write_simple(sh)
    xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    assert max(xml) < 128 and b"Caf&#233; &lt;&amp;&gt; &#8364;" in xml and b'xml:space="preserve"' in xml
    sh2, lossy = S.read_sheet(data)
    assert sh2 == sh and not lossy
    assert b'<cols><col min="2" max="3" width="14" customWidth="1"/><col min="5" max="5" width="8"' in xml


def test_simple_to_xlsx_deflates_and_keeps_everything():
    data = S.write_simple(sample_sheet())
    out = S.simple_to_xlsx(data)
    z = zipfile.ZipFile(io.BytesIO(out))
    assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist()) and z.testzip() is None
    assert len(out) < len(data)
    assert S.read_sheet(out)[0] == sample_sheet()
    with pytest.raises(ConvError):
        S.simple_to_xlsx(b"PK\x03\x04garbage")
    with pytest.raises(ConvError):
        S.simple_to_xlsx(zipped({"hello.txt": "x"}))


def test_simple_to_xlsx_keeps_more_than_7000_cells():
    sh = S.Sheet()
    for i in range(1, 12001):
        sh.cells[((i - 1) // 10 + 1, (i - 1) % 10 + 1)] = S.Cell(float(i))
    sh2, lossy = S.read_sheet(S.simple_to_xlsx(S.write_simple(sh)), limits=False)
    assert len(sh2.cells) == 12000 and not lossy
    _, lossy = S.read_sheet(S.write_simple(sh))           # OPEN path applies the limits
    assert lossy


def test_csv_both_ways():
    csv_in = "Name,Amount,Rate,Price,Zip\r\nWidget,12.5,5%,\"$1,234.50\",01234\r\n\r\n\"Grüße, \"\"x\"\"\",-3,,,\r\n"
    data, lossy = S.csv_to_simple(csv_in.encode("utf-8"))
    assert not lossy
    sh, _ = S.read_sheet(data)
    c = sh.cells
    assert c[(1, 1)] == S.Cell("Name", "s")
    assert c[(2, 2)] == S.Cell(12.5) and c[(2, 3)] == S.Cell(0.05, "n", None, 16)
    assert c[(2, 4)] == S.Cell(1234.5, "n", None, 24) and c[(2, 5)] == S.Cell("01234", "s")
    assert c[(4, 1)].v == 'Grüße, "x"' and c[(4, 2)].v == -3.0 and (3, 1) not in c
    out = S.simple_to_csv(S.write_simple(sample_sheet())).decode("utf-8")
    assert out.splitlines() == ["Item,12.5", "", ",30,#DIV/0!", ' Café <&> €,ok', ",,"]
    # semicolon CSV, unmappable chars
    data, lossy = S.csv_to_simple("a;b\n1;☃\n".encode())
    sh, _ = S.read_sheet(data)
    assert lossy and sh.cells[(2, 1)].v == 1.0 and sh.cells[(2, 2)].v == "?"


def test_csv_limits_truncate():
    rows = "\n".join(",".join(["x" * 60] * 3) for _ in range(3000))
    data, lossy = S.csv_to_simple(rows.encode())
    sh, _ = S.read_sheet(data)
    assert lossy and len(sh.cells) == S.MAX_CELLS and all(len(c.v) == 50 for c in sh.cells.values())


# ====================================================================== real xlsx -> simple

def real_xlsx():
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xml" ContentType="application/xml"/></Types>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="R1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="/xl/workbook.xml"/></Relationships>')
    wb = (f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="My Data" sheetId="1" r:id="rId7"/><sheet name="Other" sheetId="2" r:id="rId8"/>'
          '</sheets></workbook>')
    wbrels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
              'worksheet" Target="worksheets/data.xml"/>'
              '<Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
              'worksheet" Target="worksheets/other.xml"/>'
              '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
              'sharedStrings" Target="sharedStrings.xml"/>'
              '<Relationship Id="rId10" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
              'styles" Target="/xl/styles.xml"/></Relationships>')
    sst = (f'<sst xmlns="{NS}" count="4" uniqueCount="4"><si><t>Header</t></si>'
           '<si><r><rPr><b/></rPr><t>Rich </t></r><r><t>text</t></r><rPh><t>ignored</t></rPh></si>'
           '<si><t>Snow ☃ “q”_x000D_</t></si><si><t>' + "L" * 70 + '</t></si></sst>')
    styles = (f'<styleSheet xmlns="{NS}"><numFmts count="3"><numFmt numFmtId="170" formatCode="#,##0.000"/>'
              '<numFmt numFmtId="171" formatCode="[$€-407]\\ #,##0.00"/>'
              '<numFmt numFmtId="172" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
              '<fonts count="3"><font><sz val="10"/></font><font><b val="0"/></font><font><b/><i/></font></fonts>'
              '<cellXfs count="7"><xf numFmtId="0" fontId="0"/>'
              '<xf numFmtId="0" fontId="2"><alignment horizontal="right"/></xf>'
              '<xf numFmtId="170" fontId="1"/><xf numFmtId="171" fontId="0"><alignment horizontal="left"/></xf>'
              '<xf numFmtId="9" fontId="2"><alignment horizontal="center"/></xf>'
              '<xf numFmtId="172" fontId="0"/><xf numFmtId="7" fontId="0"/></cellXfs></styleSheet>')
    sheet = (f'<worksheet xmlns="{NS}"><dimension ref="A1:D9"/><cols><col min="1" max="1" width="8.7109375"/>'
             '<col min="2" max="3" width="55" customWidth="1"/><col min="300" max="400" width="12"/></cols>'
             '<sheetData>'
             '<row r="1"><c r="A1" t="s" s="1"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>'
             '<c r="D1" t="s"><v>3</v></c><c r="E1" s="6"/></row>'
             '<row r="2"><c r="A2" s="2"><v>1.5</v></c><c r="B2" t="b"><v>1</v></c><c r="C2" t="e"><v>#N/A</v></c>'
             '<c r="D2" s="3"><v>9.99</v></c></row>'
             '<row r="3"><c r="A3" s="4"><v>0.5</v></c><c r="B3" t="inlineStr"><is><t>inline</t></is></c>'
             '<c r="C3" s="5"><v>45000</v></c></row>'
             '<row r="4"><c r="A4"><f t="shared" ref="A4:C5" si="0">SUM($A$1:A3)+\'My Data\'!B$2*Other!A1</f><v>3</v></c>'
             '<c r="B4"><f t="shared" si="0"/><v>4</v></c><c r="C4"><f t="shared" si="0"/></c></row>'
             '<row r="5"><c r="A5"><f t="shared" si="0"/><v>5</v></c>'
             '<c r="B5" t="str"><f>_xlfn.CONCAT("A1",B1)</f><v>A1Rich text</v></c>'
             '<c r="C5" t="b"><f>A2&gt;1</f><v>1</v></c></row>'
             '<row r="6"><c r="A6"><f>LOG10(100)+A1</f><v>2</v></c>'
             '<c r="B6"><f>' + "1+" * 30 + '1</f><v>31</v></c></row>'
             '<row r="10000"><c r="A10000"><v>1</v></c></row>'
             '</sheetData></worksheet>')
    return zipped({"[Content_Types].xml": ct, "_rels/.rels": rels, "xl/workbook.xml": wb,
                   "xl/_rels/workbook.xml.rels": wbrels, "xl/sharedStrings.xml": sst, "xl/styles.xml": styles,
                   "xl/worksheets/data.xml": sheet, "xl/worksheets/other.xml": f'<worksheet xmlns="{NS}"/>'})


def test_real_xlsx_to_simple():
    out, lossy = S.xlsx_to_simple(real_xlsx())
    assert lossy                                     # snowman, long text, 2nd sheet, date fmt, long formula, row 10000
    z = zipfile.ZipFile(io.BytesIO(out))
    assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
    assert z.read("xl/styles.xml").decode() == S.STYLES_XML
    sh, l2 = S.read_sheet(out)
    assert not l2
    c = sh.cells
    assert c[(1, 1)] == S.Cell("Header", "s", None, 1 + 2 * 3)           # bold + right
    assert c[(1, 2)] == S.Cell("Rich text", "s")
    assert c[(1, 3)] == S.Cell('Snow ? "q" ', "s")                       # folded quotes, _x000D_ -> space
    assert c[(1, 4)].v == "L" * 50
    assert (1, 5) not in c                                               # styled empty cell dropped
    assert c[(2, 1)] == S.Cell(1.5, "n", None, 8)                        # #,##0.000 -> 0.00, <b val=0> not bold
    assert c[(2, 2)] == S.Cell(1.0)                                      # boolean -> 1
    assert c[(2, 3)] == S.Cell("#N/A", "s")                              # constant error -> text
    assert c[(2, 4)] == S.Cell(9.99, "n", None, 2 * 1 + 8 * 3)           # euro currency, left
    assert c[(3, 1)] == S.Cell(0.5, "n", None, 1 + 2 * 2 + 8 * 2)        # percent, bold, center
    assert c[(3, 2)] == S.Cell("inline", "s")
    assert c[(3, 3)] == S.Cell(45000.0)                                  # date -> number
    # shared formula expanded; first-sheet name dropped, other sheet kept
    assert c[(4, 1)] == S.Cell(3.0, "n", "SUM($A$1:A3)+B$2*Other!A1")
    assert c[(4, 2)] == S.Cell(4.0, "n", "SUM($A$1:B3)+C$2*Other!B1")
    assert c[(4, 3)] == S.Cell(None, "n", "SUM($A$1:C3)+D$2*Other!C1")
    assert c[(5, 1)] == S.Cell(5.0, "n", "SUM($A$1:A4)+B$2*Other!A2")
    assert c[(5, 2)] == S.Cell("A1Rich text", "s", 'CONCAT("A1",B1)')     # _xlfn. stripped, string kept
    assert c[(5, 3)] == S.Cell(1.0, "n", "A2>1")                         # boolean result -> number
    assert c[(6, 1)].f == "LOG10(100)+A1"
    assert c[(6, 2)] == S.Cell(31.0)                                     # formula > 50 bytes: value kept
    assert not any(r > 9999 for r, _ in c)
    assert sh.widths == {1: 9, 2: 40, 3: 40}


def test_translate_formula():
    t = S.translate_formula
    assert t("=A1+$B2+C$3+$D$4", "S", 1, 1) == "B2+$B3+D$3+$D$4"
    assert t('"A1"&A1&Sheet1!A1&\'Sheet 1\'!A1', "Sheet 1", 2, 0) == '"A1"&A3&Sheet1!A3&A3'
    assert t("A1", "S", -1, 0) == "#REF!"
    assert t("SUM(A:A)+ATAN2(1,2)", "S", 1, 1) == "SUM(A:A)+ATAN2(1,2)"


def test_xlsx_hostile_inputs():
    with pytest.raises(ConvError):
        S.xlsx_to_simple(b"not a zip")
    # zip bomb: 40 MiB of spaces in the worksheet
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{NS}"><sheets><sheet name="a"/></sheets></workbook>')
        z.writestr("xl/worksheets/sheet1.xml", b"<worksheet>" + b" " * (40 << 20) + b"</worksheet>")
    assert len(bomb.getvalue()) < 100_000
    with pytest.raises(ConvError, match="too large"):
        S.xlsx_to_simple(bomb.getvalue())
    # entity expansion
    lol = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;">]>'
           f'<workbook xmlns="{NS}"><sheets><sheet name="&b;"/></sheets></workbook>')
    with pytest.raises(ConvError, match="forbidden"):
        S.xlsx_to_simple(zipped({"xl/workbook.xml": lol}))
    with pytest.raises(ConvError):
        S.xlsx_to_simple(zipped({"word/document.xml": "<x/>"}))


def test_classify_numfmt():
    c = S.classify_numfmt
    assert c("General") == (0, False) and c("0") == (0, False) and c("#,##0.0") == (1, False)
    assert c("0%") == (2, False) and c('"$"#,##0;[Red]-"$"#,##0') == (3, False) and c("£#,##0.00") == (3, False)
    assert c("[$-409]mmm d, yyyy") == (0, True) and c("h:mm") == (0, True) and c("0.00E+00") == (0, False)
    assert c('#,##0.00" kg"') == (1, False)


# ====================================================================== WP RTF -> DOCX

def wp_rtf(lines):
    """X16 WP dialect. lines: (xwp value, pagebb, li, body)."""
    h = ("{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033\r\n{\\fonttbl{\\f0\\fmodern\\fcharset0 Courier New;}"
         "{\\f1\\fmodern\\fcharset0 Lucida Console;}{\\f2\\fmodern\\fcharset0 Andale Mono;}}\r\n{\\colortbl;"
         + "".join(f"\\red{r}\\green{g}\\blue{b};" for r, g, b in D.WP_PALETTE) + "}\r\n"
         "\\paperw12240\\paperh15840\\margl1440\\margr1440\\margt1440\\margb1080\\viewkind1\r\n")
    out = h
    for v, pb, li, body in lines:
        al = ("\\ql", "\\qc", "\\qr")[v & 3]
        fi = "\\fi-576" if (v >> 2) & 3 and not v & 128 else ""
        pbb = "\\pagebb" if pb else ""
        out += (f"\\pard\\plain\\sb0\\sa0\\sl-240\\slmult0\\ri140\\li{li}{fi}{al}{pbb}"
                f"{{\\*\\xwp{v}}}\\fs24 {body}\\par\r\n")
    return (out + "}\r\n").encode("latin-1")


WP_SAMPLE = [
    (1, False, 0, "\\b Title\\b0"),
    (0, False, 0, "Hello \\cf3 red\\cf1  and \\highlight8 yellow\\highlight0  text that"),
    (128, False, 0, "wraps \\i here\\i0  caf\\'e9 \\'a4"),
    (4, False, 576, "\\u9679?   first bullet"),
    (4 + 16, False, 1152, "\\u9675?   nested \\ul under\\ulnone"),
    (8, False, 576, "1.  one"),
    (8 + 128, False, 576, "continued"),
    (8, False, 576, "2.  two \\f2\\caps mono\\f1\\caps0  lucida"),
    (2 + 32, True, 1152, "After break"),
    (8, False, 576, "1.  again"),
    (0, False, 0, ""),
]


def doc_xml(docx: bytes):
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(io.BytesIO(docx))
    assert z.testzip() is None
    for n in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml",
              "word/numbering.xml", "word/_rels/document.xml.rels"):
        ET.fromstring(z.read(n))
    return ET.fromstring(z.read("word/document.xml")), ET.fromstring(z.read("word/numbering.xml"))


def _w(tag):
    return f"{{{W}}}{tag}"


def paras_of(root):
    out = []
    for p in root.iter(_w("p")):
        ppr = p.find(_w("pPr"))
        runs = []
        for r in p.findall(_w("r")):
            rpr = r.find(_w("rPr"))
            props = {} if rpr is None else {c.tag.split("}")[1]: (c.get(_w("fill")) or c.get(_w("val"))
                                                                   or c.get(_w("ascii")) or True) for c in rpr}
            runs.append(("".join(t.text or "" for t in r.findall(_w("t"))), props))
        pp = {}
        if ppr is not None:
            for c in ppr:
                k = c.tag.split("}")[1]
                if k == "numPr":
                    pp[k] = (c.find(_w("numId")).get(_w("val")), c.find(_w("ilvl")).get(_w("val")))
                elif k == "ind":
                    pp[k] = c.get(_w("left"))
                else:
                    pp[k] = c.get(_w("val")) or True
        out.append(("".join(t for t, _ in runs), pp, runs))
    return out


def test_wp_rtf_to_docx():
    root, num = doc_xml(D.wp_rtf_to_docx(wp_rtf(WP_SAMPLE)))
    ps = paras_of(root)
    texts = [t for t, _, _ in ps]
    assert texts == ["Title", "Hello red and yellow text that wraps here café €", "first bullet",
                     "nested under", "one continued", "two mono lucida", "After break", "again", ""]
    assert ps[0][1] == {"jc": "center"} and ps[0][2][0][1] == {"b": True}
    runs = dict(ps[1][2])
    assert runs["red"] == {"color": "880000"} and runs["yellow"] == {"shd": "EEEE77"} and runs["here"] == {"i": True}
    assert ps[2][1] == {"numPr": ("1", "0")} and ps[3][1] == {"numPr": ("1", "1")}
    assert dict(ps[3][2])["under"] == {"u": "single"}
    n1, n2 = ps[4][1]["numPr"], ps[5][1]["numPr"]
    assert n1 == n2 and n1[0] not in ("1",) and n1[1] == "0"
    r5 = dict(ps[5][2])
    assert r5["mono"] == {"rFonts": "Andale Mono"} and r5[" lucida"] == {"rFonts": "Lucida Console"}
    assert ps[6][1] == {"pageBreakBefore": True, "ind": "1152", "jc": "right"}
    n3 = ps[7][1]["numPr"]
    assert n3[0] != n1[0]                                          # numbering restarts at 1
    fmts = {a.get(_w("abstractNumId")): a.find(_w("lvl")).find(_w("numFmt")).get(_w("val"))
            for a in num.findall(_w("abstractNum"))}
    assert sorted(fmts.values()) == ["bullet", "decimal"]
    nums = {n.get(_w("numId")): n for n in num.findall(_w("num"))}
    assert {n1[0], n3[0], "1"} <= set(nums)
    so = nums[n3[0]].find(f"{_w('lvlOverride')}/{_w('startOverride')}")
    assert so.get(_w("val")) == "1"


def test_generic_rtf_to_docx():
    rtf = (b"{\\rtf1\\ansi{\\fonttbl{\\f0 Times;}{\\f1 Arial;}}{\\colortbl;\\red0\\green0\\blue255;}"
           b"\\pard\\qc\\li720 Caf\\'e9 \\f1\\cf1 blue\\par\\page Next\\line line\\par{\\*\\generator x;}}")
    ps = paras_of(doc_xml(D.wp_rtf_to_docx(rtf))[0])
    assert [t for t, _, _ in ps] == ["Café blue", "Nextline"]
    assert ps[0][1] == {"ind": "720", "jc": "center"} and dict(ps[0][2])["blue"] == {"rFonts": "Arial", "color": "0000FF"}
    assert ps[1][1].get("pageBreakBefore")
    with pytest.raises(ConvError):
        D.wp_rtf_to_docx(b"hello")
    with pytest.raises(ConvError):
        D.wp_rtf_to_docx(b"{\\rtf1" + b"{" * 200 + b"}" * 200 + b"}")


# ====================================================================== DOCX -> RTF

def make_docx(body, styles="", numbering=""):
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    drels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="a" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
             'styles" Target="styles.xml"/><Relationship Id="b" Type="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships/numbering" Target="numbering.xml"/></Relationships>')
    return zipped({"_rels/.rels": rels, "word/_rels/document.xml.rels": drels,
                   "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
                   "word/styles.xml": f'<w:styles xmlns:w="{W}">{styles}</w:styles>',
                   "word/numbering.xml": f'<w:numbering xmlns:w="{W}">{numbering}</w:numbering>'})


DOCX_STYLES = ('<w:style w:type="paragraph" w:default="1" w:styleId="Normal"/>'
               '<w:style w:type="paragraph" w:styleId="H1"><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/>'
               '</w:pPr><w:rPr><w:b/><w:color w:val="2F5496"/></w:rPr></w:style>'
               '<w:style w:type="paragraph" w:styleId="LB"><w:pPr><w:numPr><w:numId w:val="5"/></w:numPr></w:pPr></w:style>')
DOCX_NUM = ('<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
            '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/></w:lvl></w:abstractNum>'
            '<w:num w:numId="5"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="6"><w:abstractNumId w:val="1"/></w:num>')


def r(text, rpr=""):
    return f'<w:r>{"<w:rPr>" + rpr + "</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r>'


def test_docx_to_rtf():
    body = (
        f'<w:p><w:pPr><w:pStyle w:val="H1"/></w:pPr>{r("Heading “One”")}</w:p>'
        f'<w:p>{r("plain ")}{r("red", "<w:color w:val=\"FF0000\"/>")}{r(" hl", "<w:highlight w:val=\"yellow\"/>")}'
        f'{r(" bi", "<w:b/><w:i/><w:u w:val=\"single\"/>")}{r(" x", "<w:b w:val=\"0\"/>")}'
        f'{r("hidden", "<w:vanish/>")}{r(" {\\} € ☃ —")}'
        f'<w:hyperlink><w:r><w:t>link</w:t></w:r></w:hyperlink>'
        f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{r("7")}<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        f'<w:p><w:pPr><w:pStyle w:val="LB"/></w:pPr>{r("bullet")}</w:p>'
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="6"/></w:numPr></w:pPr>{r("first")}</w:p>'
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="6"/></w:numPr></w:pPr>{r("sub")}</w:p>'
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="6"/></w:numPr></w:pPr>{r("second")}</w:p>'
        f'<w:p><w:pPr><w:ind w:left="1152"/><w:jc w:val="right"/></w:pPr>{r("before")}'
        f'<w:r><w:br w:type="page"/><w:t>after</w:t><w:tab/><w:t>tab</w:t></w:r></w:p>'
        f'<w:p><w:pPr><w:pageBreakBefore/></w:pPr>{r("pb")}<w:r><w:drawing/></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p>' + r("a") + '</w:p></w:tc><w:tc><w:p>' + r("b") + '</w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p>' + r("c") + '</w:p></w:tc><w:tc><w:p>' + r("d") + '</w:p></w:tc></w:tr></w:tbl>'
        '<w:sectPr/>')
    rtf, lossy = D.docx_to_rtf(make_docx(body, DOCX_STYLES, DOCX_NUM))
    assert lossy
    s = rtf.decode("latin-1")
    assert s.startswith("{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033\r\n{\\fonttbl{\\f0\\fmodern\\fcharset0 Courier New;}")
    assert "{\\colortbl;\\red0\\green0\\blue0;\\red255\\green255\\blue255;\\red136\\green0\\blue0;" in s
    assert "\\xwp" not in s and s.endswith("}\r\n") and s.count("\\colortbl") == 1
    lines = s.split("viewkind1\r\n")[1].split("\r\n")
    assert lines[0] == '\\pard\\plain\\qc\\li0\\fs24 \\b\\cf7 Heading "One"\\par'          # dark blue -> blue
    assert lines[1] == ("\\pard\\plain\\ql\\li0\\fs24 plain \\cf3 red\\cf1\\highlight8  hl\\b\\i\\ul\\highlight0  bi"
                        "\\b0\\i0\\ulnone  x \\{\\\\\\} \\'a4 ? --link7\\par")
    assert lines[2] == "\\pard\\plain\\ql\\li0\\fs24 \\u9679?   bullet\\par"
    assert lines[3] == "\\pard\\plain\\ql\\li0\\fs24 1.  first\\par"
    assert lines[4] == "\\pard\\plain\\ql\\li576\\fs24 a.  sub\\par"
    assert lines[5] == "\\pard\\plain\\ql\\li0\\fs24 2.  second\\par"
    assert lines[6] == "\\pard\\plain\\qr\\li1152\\fs24 before\\par"
    assert lines[7] == "\\pard\\plain\\qr\\li1152\\pagebb\\fs24 after\\tab tab\\par"
    assert lines[8] == "\\pard\\plain\\ql\\li0\\pagebb\\fs24 pb\\par"
    assert lines[9:11] == ["\\pard\\plain\\ql\\li0\\fs24 a    b\\par", "\\pard\\plain\\ql\\li0\\fs24 c    d\\par"]
    text = rtf_to_text(rtf, WP_CHARSET)
    assert text.splitlines()[:3] == ['Heading "One"', "plain red hl bi x {\\} € ? --link7", "●   bullet"]


def test_docx_wp_roundtrip_and_long_paragraph():
    rtf = wp_rtf(WP_SAMPLE)
    back, lossy = D.docx_to_rtf(D.wp_rtf_to_docx(rtf))
    assert not lossy
    assert rtf_to_text(back, WP_CHARSET).splitlines() == [
        "Title", "Hello red and yellow text that wraps here café €", "●   first bullet",
        "○   nested under", "1.  one continued", "2.  two mono lucida", "After break", "1.  again"]
    long = make_docx("<w:p>" + r("word " * 500) + "</w:p>")
    rtf, lossy = D.docx_to_rtf(long)
    body = [ln for ln in rtf.split(b"\r\n") if ln.startswith(b"\\pard")]
    assert lossy and len(body) == 3 and all(len(ln) < 1100 for ln in body)


def test_docx_hostile_inputs():
    with pytest.raises(ConvError):
        D.docx_to_rtf(b"PK\x05\x06" + bytes(18))
    with pytest.raises(ConvError):
        D.docx_to_rtf(zipped({"xl/workbook.xml": "<x/>"}))
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<w:document>" + b" " * (40 << 20) + b"</w:document>")
    with pytest.raises(ConvError, match="too large"):
        D.docx_to_rtf(bomb.getvalue())
    xxe = zipped({"word/document.xml": '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                  f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>'})
    with pytest.raises(ConvError):
        D.docx_to_rtf(xxe)


def test_nearest_palette():
    assert D.nearest_palette((0, 0, 0)) == 0 and D.nearest_palette((255, 255, 255)) == 1
    assert D.nearest_palette((255, 0, 0)) == 2 and D.nearest_palette((0, 0, 255)) == 6
    for i, c in enumerate(D.WP_PALETTE):
        assert D.nearest_palette(c) == i


def test_formula_limit_matches_x16():
    """X16 Sheets keeps formulas up to 46 bytes; longer ones arrive as their cached value."""
    from x16hub.link.sheetconv import Cell, Sheet
    long_f = "SUM(A1:A2)+SUM(B1:B2)+SUM(C1:C2)+SUM(D1:D2)+100"    # 47 bytes
    ok_f = long_f[:-1]                                             # 46 bytes
    assert len(long_f) == 47
    src = S.write_simple(Sheet({(1, 1): Cell(2.0, "n", long_f, 0), (1, 2): Cell(3.0, "n", ok_f, 0)}, {}))
    data, lossy = S.xlsx_to_simple(S.simple_to_xlsx(src))
    sh, _ = S.read_sheet(data)
    assert lossy and sh.cells[(1, 1)].f is None and sh.cells[(1, 1)].v == 2.0
    assert sh.cells[(1, 2)].f == ok_f


# ---------------------------------------------------------------- memory-amplification guards


def _docx_with_body(body: bytes) -> bytes:
    from x16hub.link.ooxml import deflated_zip
    w = b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = b"<w:document " + w + b"><w:body>" + body + b"</w:body></w:document>"
    return deflated_zip([("word/document.xml", doc)])


def test_docx_element_bomb_is_refused_before_parsing():
    # ~50 KB deflated, 32 MiB of empty paragraphs: used to need ~2 GB of RAM.
    data = _docx_with_body(b"<w:p/>" * 5_500_000)
    assert len(data) < 100_000
    with pytest.raises(ConvError, match="too many elements"):
        D.docx_to_rtf(data)


def test_sparse_sheet_cannot_balloon_into_huge_csv():
    # Cells on many rows in column XFD: a ~100 KB upload used to pad out to ~33 MB of commas
    # (a 1 MiB one to ~1 GB, past the unit's MemoryMax).
    sh = S.Sheet()
    for r in range(1, 2001):
        sh.cells[(r, 16384)] = S.Cell(1.0, "n")
    with pytest.raises(ConvError, match="larger than 1 MiB"):
        S.simple_to_csv(S.write_simple(sh))
    small = S.Sheet()
    small.cells[(1, 300)] = S.Cell(1.0, "n")
    assert S.simple_to_csv(S.write_simple(small)).count(b",") == 299
