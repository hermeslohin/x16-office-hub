"""Pictures: BMX (X16 Write's bitmap format) <-> Pillow, RTF/DOCX picture conversion, .rtf PNG copies,
and .bmx files on the docs channel."""
import io
import random
import struct
import xml.etree.ElementTree as ET
import zipfile

import pytest
from PIL import Image

from test_link_server import Hub, run
from test_officeconv import W, make_docx, r, wp_rtf, zipped
from x16hub.link import bmx as B
from x16hub.link import docxconv as D
from x16hub.link import rtfpict as P
from x16hub.link.docs import FL_UNSUPPORTED, T_BMX
from x16hub.link.client import LinkError
from x16hub.link import frame as F
from x16hub.link.ooxml import ConvError
from x16hub.link.rtfconv import WP_CHARSET, rtf_to_text

WPD = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def make_bmx(bpp, w, h, pal, pixels, start=0):
    """pal: [(r4, g4, b4)], pixels: rows of indices"""
    head = struct.pack("<3sBBBHHBBHBB16s", b"BMX", 1, bpp, {1: 0, 2: 1, 4: 2, 8: 3}[bpp], w, h,
                       len(pal) & 0xFF, start, 32 + 2 * len(pal), 0, 0, bytes(16))
    pb = b"".join(bytes(((g << 4) | b, r)) for r, g, b in pal)
    data = bytearray()
    for row in pixels:
        acc, nb = 0, 0
        for v in row:
            acc, nb = (acc << bpp) | v, nb + bpp
            if nb == 8:
                data.append(acc)
                acc, nb = 0, 0
    return head + pb + bytes(data)


def noisy(w, h, seed=1):
    rnd = random.Random(seed)
    return Image.frombytes("RGB", (w, h), bytes(rnd.randrange(256) for _ in range(w * h * 3)))


def png_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


# ====================================================================== BMX

@pytest.mark.parametrize("bpp", [1, 2, 4, 8])
def test_bmx_parse_and_encode_roundtrip(bpp):
    n = 1 << bpp
    pal = [((i * 3) % 16, (i * 5) % 16, (i * 7) % 16) for i in range(n)]
    w, h = 16, 3
    pixels = [[(x + y) % n for x in range(w)] for y in range(h)]
    data = make_bmx(bpp, w, h, pal, pixels)
    img = B.bmx_to_image(data)
    assert img.mode == "RGB" and img.size == (w, h)
    for y in range(h):
        for x in range(w):
            r4, g4, b4 = pal[pixels[y][x]]
            assert img.getpixel((x, y)) == (r4 * 17, g4 * 17, b4 * 17)
    again = B.image_to_bmx(img)                 # back to BMX (8 bpp): the same picture
    hd = B.parse_header(again)
    assert again[:6] == b"BMX\x01\x08\x03" and hd["start"] == 0 and hd["off"] == 32 + 2 * hd["n"]
    assert len(again) == hd["off"] + w * h and again[14] == 0
    assert list(B.bmx_to_image(again).getdata()) == list(img.getdata())
    png, pw, ph = B.bmx_to_png(data)
    assert (pw, ph) == (w, h) and list(Image.open(io.BytesIO(png)).convert("RGB").getdata()) == list(img.getdata())


def test_bmx_palette_start_and_out_of_range_black():
    # 4 entries starting at index 2: indices 0, 1 and 6+ are black
    data = make_bmx(8, 8, 1, [(15, 0, 0), (0, 15, 0), (0, 0, 15), (15, 15, 15)], [[0, 1, 2, 3, 4, 5, 6, 255]], start=2)
    px = list(B.bmx_to_image(data).getdata())
    assert px == [(0, 0, 0), (0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0), (0, 0, 0)]
    # 0 entries = 256
    full = make_bmx(8, 8, 1, [(i % 16, 0, 0) for i in range(256)], [list(range(8))])
    assert full[10] == 0 and B.parse_header(full)["n"] == 256


def test_bmx_malformed_rejected():
    good = make_bmx(8, 8, 2, [(1, 2, 3)], [[0] * 8, [0] * 8])
    B.parse_header(good)

    def patched(off, fmt, v):
        b = bytearray(good)
        struct.pack_into(fmt, b, off, v)
        return bytes(b)
    bad = [b"", good[:31], b"BMP" + good[3:], patched(3, "B", 2), patched(4, "B", 3), patched(4, "B", 16),
           patched(14, "B", 1), patched(6, "<H", 0), patched(6, "<H", 1025), patched(8, "<H", 0),
           patched(8, "<H", 2000), good[:-1], patched(12, "<H", 33), patched(5, "B", 4),
           make_bmx(8, 8, 1, [(0, 0, 0), (1, 1, 1)], [[0] * 8], start=255),      # palette past index 255
           make_bmx(1, 8, 1, [(0, 0, 0)], [[0] * 8])[:-1]]
    b4 = bytearray(make_bmx(4, 4, 1, [(0, 0, 0)], [[0, 0, 0, 0]]))
    struct.pack_into("<H", b4, 6, 3)                     # 3 x 4 bpp: half a byte per line
    bad.append(bytes(b4))
    for b in bad:
        with pytest.raises(ConvError):
            B.bmx_to_image(b)
        with pytest.raises(ConvError):
            B.bmx_to_png(b)


def test_image_to_bmx_rules():
    big = B.image_to_bmx(noisy(640, 400))
    hd = B.parse_header(big)
    assert (hd["w"], hd["h"]) == (320, 200) and hd["n"] <= 256 and hd["bpp"] == 8
    assert len(big) == 32 + 2 * hd["n"] + 320 * 200
    tall = B.parse_header(B.image_to_bmx(Image.new("RGB", (100, 960), "red")))
    assert (tall["w"], tall["h"]) == (25, 240)
    small = B.image_to_bmx(Image.new("RGB", (10, 7), (18, 52, 86)))      # never scaled up
    assert B.parse_header(small)["w"] == 10 and B.parse_header(small)["n"] == 1
    assert B.bmx_to_image(small).getpixel((0, 0)) == (17, 51, 85)           # 12-bit colour
    clear = Image.new("RGBA", (4, 4), (255, 0, 0, 0))
    clear.putpixel((0, 0), (0, 0, 255, 255))
    out = B.bmx_to_image(B.image_to_bmx(clear))
    assert out.getpixel((1, 1)) == (255, 255, 255) and out.getpixel((0, 0)) == (0, 0, 255)
    for v in B.bmx_to_image(big).getdata():
        assert all(c % 17 == 0 for c in v)


def test_picture_to_bmx_untrusted():
    assert B.parse_header(B.picture_to_bmx(png_bytes(noisy(50, 40))))["w"] == 50
    assert B.parse_header(B.picture_to_bmx(png_bytes(noisy(1200, 900), "JPEG")))["w"] == 320
    assert B.parse_header(B.picture_to_bmx(png_bytes(Image.new("P", (9, 9)), "GIF")))["w"] == 9
    assert B.parse_header(B.picture_to_bmx(png_bytes(Image.new("RGB", (9, 9)), "BMP")))["w"] == 9
    for junk in (b"", b"not a picture", png_bytes(noisy(20, 20))[:60], b"%PDF-1.4"):
        with pytest.raises(ConvError):
            B.picture_to_bmx(junk)
    # a decompression bomb: tiny file, 30000 x 30000 pixels declared
    bomb = png_bytes(Image.new("1", (30000, 30000)))
    assert len(bomb) < 200_000
    with pytest.raises(ConvError, match="too large"):
        B.picture_to_bmx(bomb)
    with pytest.raises(ConvError, match="too large"):               # under Pillow's own limit, over ours
        B.picture_to_bmx(png_bytes(Image.new("1", (6000, 6000))))


def test_size_rule():
    assert B.display_inches(320, 240) == (4.0, 3.0)
    w, h = B.display_inches(992, 100)                    # f = 496/992 = 0.5
    assert (w, h) == (6.2, 0.625)
    w, h = B.display_inches(100, 1000)                   # f = 640/1000
    assert (w, h) == pytest.approx((0.8, 8.0))
    assert B.display_emu(320, 240) == (3657600, 2743200) and B.display_twips(320, 240) == (5760, 4320)


# ====================================================================== X16 Write RTF <-> DOCX

def wp_with_picture(pic: bytes) -> bytes:
    rtf = wp_rtf([(0, False, 0, "Before"), (0, False, 0, "After")])
    i = rtf.index(b"\\pard", rtf.index(b"Before"))
    return rtf[:i] + B.rtf_group(pic) + b"\r\n" + rtf[i:]


def test_wp_rtf_picture_to_docx_and_back():
    pic = make_bmx(8, 992, 100, [(15, 0, 0), (0, 0, 15)], [[(x // 10) % 2 for x in range(992)]] * 100)
    rtf = wp_with_picture(pic)
    assert b"\r\n" in rtf[rtf.index(b"xwpbmx"):rtf.index(b"xwpbmx") + 300]
    dx = D.wp_rtf_to_docx(rtf)
    z = zipfile.ZipFile(io.BytesIO(dx))
    assert z.testzip() is None
    ct = ET.fromstring(z.read("[Content_Types].xml"))
    assert any(e.get("Extension") == "png" and e.get("ContentType") == "image/png" for e in ct)
    rels = {e.get("Id"): e for e in ET.fromstring(z.read("word/_rels/document.xml.rels"))}
    root = ET.fromstring(z.read("word/document.xml"))
    ps = list(root.iter(f"{{{W}}}p"))
    assert ["".join(t.text for t in p.iter(f"{{{W}}}t")) for p in ps] == ["Before", "", "After"]
    pp = ps[1]
    assert pp.find(f"{{{W}}}pPr/{{{W}}}jc").get(f"{{{W}}}val") == "center"
    ext = pp.find(f".//{WPD}inline/{WPD}extent")
    assert (int(ext.get("cx")), int(ext.get("cy"))) == (round(6.2 * 914400), round(0.625 * 914400))
    rid = pp.find(f".//{A}blip").get(f"{RNS}embed")
    assert rels[rid].get("Type").endswith("/image") and rels[rid].get("Target") == "media/image1.png"
    img = Image.open(io.BytesIO(z.read("word/media/image1.png")))
    assert img.size == (992, 100) and img.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    # and back: the picture comes back as an xwpbmx group between the two paragraphs
    back, lossy = D.docx_to_rtf(dx)
    assert not lossy
    lines = back.split(b"\r\n")
    k = next(i for i, ln in enumerate(lines) if ln.startswith(b"{\\*\\xwpbmx "))
    after = next(ln for ln in lines[k:] if ln.startswith(b"\\pard"))
    assert lines[k - 1].endswith(b"Before\\par") and after.endswith(b"After\\par")
    grp = back[back.index(b"{\\*\\xwpbmx "):]
    grp = grp[:grp.index(b"}")]
    bm = bytes.fromhex(grp[len(b"{\\*\\xwpbmx "):].decode().replace("\r\n", ""))
    assert B.parse_header(bm)["w"] == 320 and B.parse_header(bm)["h"] in (32, 33)
    assert rtf_to_text(back, WP_CHARSET).splitlines() == ["Before", "After"]


def test_wp_rtf_bad_picture_is_dropped_not_fatal():
    rtf = wp_with_picture(b"BMX\x01garbage")
    z = zipfile.ZipFile(io.BytesIO(D.wp_rtf_to_docx(rtf)))
    assert b"w:drawing" not in z.read("word/document.xml") and not any("media" in n for n in z.namelist())
    assert b'Extension="png"' not in z.read("[Content_Types].xml")
    rtf = wp_with_picture(b"zz")                              # not even hex
    assert b"Before" in zipfile.ZipFile(io.BytesIO(D.wp_rtf_to_docx(rtf))).read("word/document.xml")


DOC_RELS = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/")


def docx_with_media(body: str, media: dict) -> bytes:
    """media: rId -> (part name under word/, bytes)"""
    base = zipfile.ZipFile(io.BytesIO(make_docx(body)))
    entries = {n: base.read(n) for n in base.namelist()}
    rels = entries["word/_rels/document.xml.rels"].decode()
    rels = rels.replace("</Relationships>", "".join(
        f'<Relationship Id="{rid}" Type="{DOC_RELS}image" Target="{name}"/>' for rid, (name, _) in media.items())
        + "</Relationships>")
    entries["word/_rels/document.xml.rels"] = rels
    doc = entries["word/document.xml"].decode().replace(
        "<w:document ", f'<w:document xmlns:r="{DOC_RELS[:-1]}" xmlns:a="http://schemas.openxmlformats.org/drawingml'
        '/2006/main" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" ')
    entries["word/document.xml"] = doc
    for name, data in media.values():
        entries["word/" + name] = data
    return zipped(entries)


def drawing(rid):
    return (f'<w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><a:graphic><a:graphicData>'
            f'<a:blip r:embed="{rid}"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')


def groups_of(rtf: bytes) -> list[bytes]:
    out = []
    for part in rtf.split(b"{\\*\\xwpbmx ")[1:]:
        out.append(bytes.fromhex(part[:part.index(b"}")].decode().replace("\r\n", "")))
    return out


def test_docx_pictures_to_rtf_split_paragraphs():
    png = png_bytes(Image.new("RGB", (40, 30), (0, 255, 0)))
    jpg = png_bytes(Image.new("RGB", (64, 48), (0, 0, 255)), "JPEG")
    body = (f'<w:p><w:pPr><w:jc w:val="right"/><w:ind w:left="576"/></w:pPr>{r("text before ")}{drawing("rP")}'
            f'{r(" text after")}</w:p>'
            f'<w:p>{drawing("rJ")}</w:p>'
            f'<w:p>{r("vml")}<w:r><w:pict><v:shape><v:imagedata r:id="rP"/></v:shape></w:pict></w:r></w:p>'
            f'<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps">{drawing("rJ")[5:-6]}</mc:Choice>'
            f'<mc:Fallback><w:pict><v:shape><v:imagedata r:id="rJ"/></v:shape></w:pict></mc:Fallback>'
            f'</mc:AlternateContent></w:r></w:p>'
            f'<w:p>{r("end")}</w:p>')
    rtf, lossy = D.docx_to_rtf(docx_with_media(body, {"rP": ("media/a.png", png), "rJ": ("media/b.jpeg", jpg)}))
    assert not lossy
    s = rtf.split(b"viewkind1\r\n")[1]
    lines = [ln for ln in s.split(b"\r\n") if ln and not ln[:1].isalnum()]
    kinds = [b"PIC" if ln.startswith(b"{\\*\\xwpbmx") else ln for ln in lines]
    assert kinds == [b"\\pard\\plain\\qr\\li576\\fs24 text before \\par", b"PIC",
                     b"\\pard\\plain\\qr\\li576\\fs24  text after\\par",
                     b"PIC",
                     b"\\pard\\plain\\ql\\li0\\fs24 vml\\par", b"PIC",
                     b"PIC",                                     # AlternateContent: one picture, not two
                     b"\\pard\\plain\\ql\\li0\\fs24 end\\par", b"}"]
    g = groups_of(rtf)
    assert [B.parse_header(x)["w"] for x in g] == [40, 64, 40, 64]
    assert B.bmx_to_image(g[0]).getpixel((0, 0)) == (0, 255, 0)
    assert B.bmx_to_image(g[1]).getpixel((5, 5))[2] > 200
    assert rtf_to_text(rtf, WP_CHARSET).splitlines() == ["text before", " text after", "vml", "end"]


def test_docx_pictures_lossy_cases():
    png = png_bytes(Image.new("RGB", (4, 4)))
    body = (f'<w:p>{r("a")}{drawing("rX")}</w:p>'                           # missing relationship
            f'<w:p>{r("b")}{drawing("rE")}</w:p>'                           # EMF: Pillow can't
            '<w:tbl><w:tr><w:tc><w:p>' + drawing("rP") + '</w:p></w:tc></w:tr></w:tbl>'    # picture in a table
            f'<w:p>{r("c")}</w:p>')
    rtf, lossy = D.docx_to_rtf(docx_with_media(body, {"rP": ("media/p.png", png), "rE": ("media/e.emf", b"\x01\x00")}))
    assert lossy and b"xwpbmx" not in rtf
    assert rtf_to_text(rtf, WP_CHARSET).splitlines() == ["a", "b", "", "c"]


def test_docx_picture_budget():
    media = {f"r{k}": (f"media/n{k}.png", png_bytes(noisy(320, 240, k))) for k in range(3)}
    body = "".join(f"<w:p>{drawing(rid)}</w:p>" for rid in media) + f'<w:p>{r("x")}</w:p>'
    rtf, lossy = D.docx_to_rtf(docx_with_media(body, media))
    g = groups_of(rtf)
    assert lossy and len(g) == 2 and sum(map(len, g)) <= 160_000 and len(g[0]) > 70_000
    rtf, lossy = D.docx_to_rtf(docx_with_media("".join(f"<w:p>{drawing(rid)}</w:p>" for rid in list(media)[:2]), media))
    assert not lossy and len(groups_of(rtf)) == 2


# ====================================================================== .rtf files on ownCloud

def x16_rtf_with_picture():
    pic = B.image_to_bmx(noisy(64, 40))
    return wp_with_picture(pic), pic


def test_rtf_png_copy_roundtrip_exact():
    rtf, pic = x16_rtf_with_picture()
    saved = P.add_png(rtf, 1 << 20)
    assert saved != rtf and saved.count(b"{\\*\\xwppng}") == 1
    i = saved.index(b"{\\*\\xwppng}")
    head = saved[i - 17:i]
    assert head.endswith(b"}\r\n\\pard\\plain\\qc")
    assert b"{\\pict\\pngblip\\picw64\\pich40\\picwgoal1152\\pichgoal720 89504e47" in saved
    hx = saved[saved.index(b"\\pichgoal720 ") + 13:]
    png = bytes.fromhex(hx[:hx.index(b"}")].decode().replace("\r\n", ""))
    assert Image.open(io.BytesIO(png)).size == (64, 40)
    back, lossy = P.for_x16(saved)
    assert back == rtf and not lossy
    # no pictures = no change at all
    plain = wp_rtf([(0, False, 0, "only text")])
    assert P.add_png(plain) == plain and P.for_x16(plain) == (plain, False)
    # a bad BMX gets no PNG; a size limit leaves PNGs out
    bad = wp_with_picture(b"BMX\x01nope")
    assert P.add_png(bad) == bad
    assert P.add_png(rtf, len(rtf) + 10) == rtf


def word_rtf(*paras: bytes) -> bytes:
    return (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Times New Roman;}}\r\n"
            + b"\r\n".join(paras) + b"\r\n}")


def shppict(img_bytes, blip=b"pngblip"):
    hx = img_bytes.hex().encode()
    return (b"{\\*\\shppict{\\pict{\\*\\picprop\\shplid1025{\\sp{\\sn shapeType}{\\sv 75}}}\\picscalex100\\picw100"
            b"\\pich100\\" + blip + b"\\bliptag1{\\*\\blipuid 0123456789abcdef0123456789abcdef}\r\n"
            + b"\r\n".join(hx[k:k + 128] for k in range(0, len(hx), 128)) + b"}}"
            b"{\\nonshppict{\\pict\\picw100\\pich100\\wmetafile8 0100090000}}")


def test_word_rtf_pictures_get_xwpbmx_groups():
    png = png_bytes(Image.new("RGB", (30, 20), (255, 0, 0)))
    jpg = png_bytes(Image.new("RGB", (500, 500), (0, 0, 255)), "JPEG")
    p1 = b"\\pard\\plain\\ql Hello\\par"
    p2 = b"\\pard\\plain\\qc {" + shppict(png) + b"}\\par"
    p3 = b"\\pard\\plain\\ql Text {\\b around} " + shppict(jpg, b"jpegblip") + b" more\\par"
    p4 = b"\\pard\\plain\\ql End\\par"
    src = word_rtf(p1, p2, p3, p4)
    out, lossy = P.for_x16(src)
    assert not lossy
    g = groups_of(out)
    assert [(B.parse_header(x)["w"], B.parse_header(x)["h"]) for x in g] == [(30, 20), (240, 240)]
    assert B.bmx_to_image(g[0]).getpixel((0, 0)) == (255, 0, 0)
    # the picture-only paragraph is replaced by the group; the text paragraph keeps its text, the group follows
    assert out.startswith(src[:src.index(p2)] + b"{\\*\\xwpbmx ")
    assert b"\\pard\\plain\\ql Text {\\b around}  more\\par\r\n{\\*\\xwpbmx " in out
    assert out.endswith(b"}\r\n" + p4 + b"\r\n}") and b"\\pict" not in out
    assert rtf_to_text(out).splitlines() == ["Hello", "Text around  more", "End"]
    # opened again: nothing more to do
    assert P.for_x16(out) == (out, False)
    # through the DOCX path X16 Write's RTF gives the same pictures
    dx = D.wp_rtf_to_docx(out)
    assert zipfile.ZipFile(io.BytesIO(dx)).read("word/document.xml").count(b"<w:drawing>") == 2


def test_word_rtf_unconvertible_and_budget():
    wmf_only = b"\\pard {\\pict\\wmetafile8\\picw10\\pich10 010009000003}\\par"
    src = word_rtf(b"\\pard A\\par", wmf_only)
    assert P.for_x16(src) == (src, True)
    big = [b"\\pard {" + shppict(png_bytes(noisy(320, 240, k))) + b"}\\par" for k in range(3)]
    out, lossy = P.for_x16(word_rtf(*big))
    assert lossy and len(groups_of(out)) == 2 and out.count(b"\\pngblip") == 1
    # pictures already sent as xwpbmx count against the budget
    pre = B.rtf_group(B.image_to_bmx(noisy(320, 240, 9)))
    out, lossy = P.for_x16(word_rtf(b"\\pard x\\par", pre, *big))
    assert lossy and len(groups_of(out)) == 2


def test_rtf_to_text_and_docx_ignore_png_copies():
    rtf, _ = x16_rtf_with_picture()
    saved = P.add_png(rtf)
    assert rtf_to_text(rtf, WP_CHARSET).splitlines() == ["Before", "After"]
    assert "Before" in rtf_to_text(saved, WP_CHARSET) and "8950" not in rtf_to_text(saved, WP_CHARSET)


# ====================================================================== docs channel

def test_docs_rtf_save_adds_png_open_removes_it():
    async def t():
        async with Hub() as h:
            rtf, _ = x16_rtf_with_picture()
            c = await h.client()
            await c.save("pic.rtf", rtf, conv=1)
            stored = h.oc.files["Documents/pic.rtf"][0]
            assert b"{\\*\\xwppng}{\\pict\\pngblip" in stored and b"{\\*\\xwpbmx " in stored
            info, data = await c.get("pic.rtf", conv=1)
            assert data == rtf and not info["flags"] & 2
            info, data = await c.get("pic.rtf", conv=0)          # native: the file as stored
            assert data == stored
            await c.save("raw.rtf", rtf, conv=0)
            assert h.oc.files["Documents/raw.rtf"][0] == rtf
            await c.close()
    run(t())


def test_docs_bmx_type():
    async def t():
        async with Hub() as h:
            pic = B.image_to_bmx(noisy(16, 16))
            h.oc.add("Documents/cat.bmx", pic)
            h.oc.add("Documents/a.rtf", b"{\\rtf1}")
            c = await h.client()
            total, es = await c.list(page=50)
            e = {x.name: x for x in es}["cat.bmx"]
            assert e.type == T_BMX == 7 and not e.flags & FL_UNSUPPORTED and e.size == len(pic)
            total, es = await c.dir("")
            e = {x.name: x for x in es}["cat.bmx"]
            assert e.type == 7 and not e.flags & FL_UNSUPPORTED
            info, data = await c.get("cat.bmx", conv=3)
            assert data == pic and info["type"] == 7
            for conv in (0, 1, 2):
                with pytest.raises(LinkError) as ex:
                    await c.open("cat.bmx", conv)
                assert ex.value.code == F.E_UNSUPPORTED
                with pytest.raises(LinkError) as ex:
                    await c.save("new.bmx", pic, conv=conv)
                assert ex.value.code == F.E_BAD_NAME
            await c.save("copy.bmx", pic, conv=3)
            assert h.oc.files["Documents/copy.bmx"][0] == pic
            await c.close()
    run(t())
