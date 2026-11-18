"""X16Link server: handshake, robustness (garbage, sizes, CRC, auth, slow clients) and docs ops."""
import asyncio
import os
import random
import struct

import pytest

from fake_oc import FakeOC
from x16hub.link import frame as F
from x16hub.link import keys as K
from x16hub.link.client import Client, LinkError
from x16hub.link.docs import FL_LOSSY, FL_UNSUPPORTED
from x16hub.link.server import Limits, LinkServer

KEY = bytes(range(32))
DEV = "x16-test"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


class Hub:
    def __init__(self, **lim):
        self.oc = FakeOC()
        self.oc.ensure_folder("Documents")
        self.keys = {DEV: KEY}
        limits = Limits(**{"handshake": 2, "idle": 3, "partial": 1, "resync_quiet": 0.05, **lim})
        self.srv = LinkServer(keys_loader=lambda: self.keys, oc_factory=lambda: self.oc, limits=limits)

    async def __aenter__(self):
        s = await self.srv.start("127.0.0.1", 0)
        self.port = s.sockets[0].getsockname()[1]
        self._s = s
        return self

    async def __aexit__(self, *a):
        self._s.close()
        await self._s.wait_closed()

    async def client(self, dev=DEV, key=KEY, maxrx=512):
        return await Client.connect("127.0.0.1", self.port, dev, key, maxrx)

    async def raw(self):
        return await asyncio.open_connection("127.0.0.1", self.port)


async def closed(r, timeout=3):
    """True if the server closes the connection (after optional bytes)."""
    try:
        while True:
            b = await asyncio.wait_for(r.read(4096), timeout)
            if not b:
                return True
    except asyncio.TimeoutError:
        return False


# ------------------------------------------------------------------ handshake / auth

def test_handshake_noop_time_bye():
    async def t():
        async with Hub() as h:
            c = await h.client()
            assert await c.noop(b"ping") == b"ping"
            tm = await c.time()
            assert tm["abbr"] in ("EST", "EDT") and tm["utcoff"] in (-300, -240) and 2026 <= tm["year"]
            await c.close()
    run(t())


def test_wrong_key_and_unknown_device_same_error_and_close():
    async def t():
        async with Hub() as h:
            for dev, key in ((DEV, bytes(32)), ("nobody", KEY)):
                with pytest.raises(LinkError) as e:
                    await h.client(dev, key)
                assert e.value.code == F.E_AUTH_FAIL and e.value.msg == "authentication failed"
    run(t())


def test_auth_lockout_after_5_failures():
    async def t():
        async with Hub() as h:
            for _ in range(5):
                with pytest.raises(LinkError):
                    await h.client(DEV, bytes(32))
            # now even the right key is refused at connect time
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await h.client()
    run(t())


def test_replayed_auth_is_useless():
    async def t():
        async with Hub() as h:
            # record a full handshake, then replay the client's bytes on a new connection
            r, w = await h.raw()
            cn = os.urandom(16)
            hello = F.encode(0, 1, F.OP_HELLO, F.MAGIC + b"\x01" + struct.pack("<H", 512) + F.pstr(DEV) + cn)
            w.write(hello)
            await w.drain()
            hdr = await r.readexactly(2)
            rep, _ = F.decode(hdr + await r.readexactly(3 + struct.unpack("<H", hdr)[0] + 2))
            sn = rep.payload[3:19]
            auth = F.encode(0, 2, F.OP_AUTH, K.client_tag(KEY, K.transcript(DEV.encode(), cn, sn)))
            w.write(auth)
            await w.drain()
            w.close()
            r2, w2 = await h.raw()
            w2.write(hello + auth)
            await w2.drain()
            data = b""
            while True:
                b = await asyncio.wait_for(r2.read(4096), 3)
                if not b:
                    break
                data += b
            assert bytes([F.OP_ERR]) in data and b"authentication failed" in data
    run(t())


def test_garbage_before_auth_closes():
    async def t():
        async with Hub() as h:
            r, w = await h.raw()
            w.write(b"ATDT5551234\r\nGET / HTTP/1.0\r\n\r\n")
            await w.drain()
            assert await closed(r)
    run(t())


def test_ops_before_auth_refused():
    async def t():
        async with Hub() as h:
            r, w = await h.raw()
            w.write(F.encode(F.CH_DOCS, 1, F.OP_LIST, struct.pack("<HB", 0, 10)))
            await w.drain()
            assert await closed(r)
    run(t())


def test_silent_client_times_out():
    async def t():
        async with Hub() as h:
            r, w = await h.raw()
            assert await closed(r, timeout=5)  # handshake deadline 2 s
    run(t())


def test_slowloris_partial_frame_times_out():
    async def t():
        async with Hub(handshake=30) as h:
            r, w = await h.raw()
            w.write(b"\x10")  # first byte of a length, then nothing
            await w.drain()
            assert await closed(r, timeout=5)  # partial timeout 1 s
    run(t())


# ------------------------------------------------------------------ robustness after auth

def test_bad_crc_then_recover_and_too_big():
    async def t():
        async with Hub() as h:
            c = await h.client()
            bad = bytearray(F.encode(0, 50, F.OP_NOOP, b"x"))
            bad[-1] ^= 0xFF
            c.w.write(bytes(bad))
            await c.w.drain()
            fr = await c.recv()
            assert fr.op == F.OP_ERR and fr.payload[0] == F.E_BAD_CRC
            assert await c.noop(b"ok") == b"ok"
            c.w.write(struct.pack("<H", 4000) + bytes(20))
            await c.w.drain()
            fr = await c.recv()
            assert fr.op == F.OP_ERR and fr.payload[0] == F.E_TOO_BIG
            assert await c.noop(b"still") == b"still"
            await c.close()
    run(t())


def test_three_bad_frames_close():
    async def t():
        async with Hub() as h:
            c = await h.client()
            for _ in range(3):
                bad = bytearray(F.encode(0, 9, F.OP_NOOP, b"x"))
                bad[-1] ^= 0xFF
                c.w.write(bytes(bad))
                await c.w.drain()
                await asyncio.sleep(0.2)
            assert await closed(c.r)
    run(t())


def test_trailing_plusplusplus_is_a_clean_hangup():
    async def t():
        async with Hub() as h:
            c = await h.client()
            assert await c.noop() == b""
            c.w.write(b"+++")
            await c.w.drain()
            assert await closed(c.r)
            await asyncio.sleep(0.1)
            assert sum(h.srv.active.values()) == 0
    run(t())


def test_frame_with_len_0x2b_is_not_mistaken_for_hangup():
    async def t():
        async with Hub() as h:
            c = await h.client()
            assert await c.noop(b"A" * 43) == b"A" * 43   # LEN = 2B 00
            await c.close()
    run(t())


def test_retransmitted_request_is_not_executed_twice():
    async def t():
        async with Hub() as h:
            c = await h.client()
            await c.save("dup.rtf", b"{\\rtf1 x}")
            # resend the COMMIT (same seq): must replay the cached reply, not write again
            n_before = len(h.oc.files)
            etag_before = h.oc.files["Documents/dup.rtf"][1]
            c.w.write(F.encode(F.CH_DOCS, c.seq, F.OP_COMMIT))
            await c.w.drain()
            fr = await c.recv()
            assert fr.op == F.OP_COMMIT | F.REPLY
            assert len(h.oc.files) == n_before and h.oc.files["Documents/dup.rtf"][1] == etag_before
            await c.close()
    run(t())


def test_unknown_channel_and_op():
    async def t():
        async with Hub() as h:
            c = await h.client()
            for ch, op in ((9, 1), (0, 0x55), (1, 0x7E)):
                with pytest.raises(LinkError) as e:
                    await c.request(ch, op)
                assert e.value.code == F.E_BAD_OP
            await c.close()
    run(t())


def test_fuzz_authenticated_session_survives():
    """Random well-framed requests: the hub must always answer (reply or ERR) or close cleanly."""
    async def t():
        rng = random.Random(42)
        async with Hub() as h:
            h.oc.add("Documents/a.rtf", b"{\\rtf1 a}")
            c = await h.client()
            for i in range(400):
                chan = rng.choice([0, 1, 1, 1, 2, 7])
                op = rng.choice([1, 2, 3, 4, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, rng.randrange(256)])
                if op == F.OP_BYE:
                    op = 0x7E
                payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 60)))
                try:
                    await c.request(chan, op, payload)
                except LinkError as e:
                    assert 1 <= e.code <= 16
            assert await c.noop(b"alive") == b"alive"
            await c.close()
    run(t())


def test_connection_caps():
    async def t():
        async with Hub(max_conns_per_ip=2) as h:
            a = await h.client()
            b = await h.client()
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await h.client()
            await a.close()
            await b.close()
    run(t())


def test_allow_list_drops_other_networks():
    async def t():
        h = Hub()
        h.srv.allow = K.parse_allow("10.0.0.0/8")
        async with h:
            r, w = await h.raw()
            assert await closed(r)
    run(t())


# ------------------------------------------------------------------ docs channel

def test_list_paginated_and_types():
    async def t():
        async with Hub() as h:
            h.oc.add("Documents/Example.odt", b"PK..")
            h.oc.add("Documents/b.TXT", "caf\u00e9\n".encode())
            h.oc.add("Documents/Sub/deep.rtf", b"{\\rtf1 d}")
            h.oc.add("Documents/snow\u2603.txt", b"x")
            for i in range(30):
                h.oc.add(f"Documents/f{i:02d}.rtf", b"{\\rtf1 %d}" % i)
            h.oc.add("Photos/p.jpg", b"jpg")
            c = await h.client(maxrx=128)          # small maxrx forces many pages
            total, items = await c.list(page=50)
            assert total == len(items) == 34
            names = [e.name for e in items]
            assert "Sub/deep.rtf" in names and "p.jpg" not in " ".join(names)
            by = {e.name: e for e in items}
            assert by["Example.odt"].type == 0 and by["Example.odt"].flags & FL_UNSUPPORTED
            assert by["b.TXT"].type == 2 and not by["b.TXT"].flags & FL_UNSUPPORTED
            snow = [e for e in items if e.name.startswith("snow")][0]
            assert snow.flags & FL_LOSSY and snow.flags & FL_UNSUPPORTED
            await c.close()
    run(t())


def test_list_creates_documents_folder():
    async def t():
        async with Hub() as h:
            h.oc.dirs.discard("Documents")
            c = await h.client()
            total, _ = await c.list()
            assert total == 0 and "Documents" in h.oc.dirs
            await c.close()
    run(t())


def test_open_rtf_passthrough_and_txt_conversion():
    async def t():
        async with Hub() as h:
            rtf = bytes(range(256)) * 9            # binary-safe, > one chunk
            h.oc.add("Documents/x.rtf", rtf)
            h.oc.add("Documents/t.txt", "Gr\u00fc\u00dfe \u20ac {x}\n".encode())
            c = await h.client()
            info, data = await c.get("x.rtf")
            assert data == rtf and info["type"] == 1 and info["etag"]
            info, data = await c.get("t.txt", conv=0)
            assert data == "Gr\u00fc\u00dfe \u20ac {x}\n".encode("iso8859_15")
            info, data = await c.get("t.txt", conv=1)
            assert data.startswith(b"{\\rtf1") and b"Gr\\'fc\\'dfe \\'a4 \\{x\\}\\par" in data
            for bad in ("../secrets/owncloud.json", "a/../../x.rtf", "/etc/passwd", ".hidden.rtf", "a\\b.rtf",
                        "x\x01.rtf"):
                with pytest.raises(LinkError) as e:
                    await c.open(bad)
                assert e.value.code == F.E_BAD_NAME, bad
            with pytest.raises(LinkError) as e:
                await c.open("Example.odt")
            assert e.value.code == F.E_UNSUPPORTED
            with pytest.raises(LinkError) as e:
                await c.open("missing.rtf")
            assert e.value.code == F.E_NOT_FOUND
            with pytest.raises(LinkError) as e:
                await c.request(F.CH_DOCS, F.OP_READ, struct.pack("<IH", 10**6, 100))
            assert e.value.code == F.E_BAD_ARG
            await c.close()
    run(t())


def test_save_create_conflict_overwrite_newname():
    async def t():
        async with Hub() as h:
            c = await h.client()
            doc = b"{\\rtf1\\ansi hello\\par}" * 50
            res = await c.save("new.rtf", doc)
            assert h.oc.files["Documents/new.rtf"][0] == doc and res["size"] == len(doc)
            # someone else edits it in ownCloud
            other = h.oc.add("Documents/new.rtf", b"{\\rtf1 web edit}")
            doc2 = doc + b"more"
            # create-only -> exists
            with pytest.raises(LinkError) as e:
                await c.save("new.rtf", doc2)
            assert e.value.code == F.E_CONFLICT and e.value.extra[0] == F.CONFLICT_EXISTS
            cur = e.value.extra[2:2 + e.value.extra[1]].decode()
            assert cur == other
            # overwrite with a stale etag -> changed
            with pytest.raises(LinkError) as e:
                await c.save("new.rtf", doc2, mode=1, etag=res["etag"])
            assert e.value.code == F.E_CONFLICT and e.value.extra[0] == F.CONFLICT_CHANGED
            # user picks overwrite: SAVE again with the current etag, data already at the hub
            res2 = await c.save("new.rtf", doc2, mode=1, etag=cur)
            assert h.oc.files["Documents/new.rtf"][0] == doc2
            # user picks new name instead (on another doc)
            res3 = await c.save("Sub2.rtf", doc2)
            assert res3["size"] == len(doc2) and res2["etag"] != cur
            await c.close()
    run(t())


def test_save_txt_conversions():
    async def t():
        async with Hub() as h:
            c = await h.client()
            await c.save("plain.txt", "\u20ac uro\n".encode("iso8859_15"), conv=0)
            assert h.oc.files["Documents/plain.txt"][0] == "\u20ac uro\n".encode()
            rtf, _ = __import__("x16hub.link.rtfconv", fromlist=["x"]).text_to_rtf("line one\nline two\n")
            await c.save("fromwp.txt", rtf, conv=1)
            assert h.oc.files["Documents/fromwp.txt"][0] == b"line one\nline two\n"
            with pytest.raises(LinkError) as e:
                await c.save("evil.sh", b"x")
            assert e.value.code == F.E_BAD_NAME
            with pytest.raises(LinkError) as e:
                await c.save("../../escape.rtf", b"x")
            assert e.value.code == F.E_BAD_NAME
            with pytest.raises(LinkError) as e:
                await c.save("nofolder/x.rtf", b"x")
            assert e.value.code == F.E_BAD_NAME
            await c.close()
    run(t())


def test_upload_resume_after_disconnect_and_checksum():
    async def t():
        async with Hub() as h:
            data = os.urandom(5000)
            c = await h.client()
            r = await c.save("big.rtf", data, stop_after=3)
            assert 0 < r["have"] < len(data)
            c.w.close()                          # connection drops mid-upload
            c2 = await h.client()
            p = await c2.request(F.CH_DOCS, F.OP_SAVE, bytes([0, 0]) + struct.pack("<II", len(data), F.crc32(data))
                                 + F.pstr("big.rtf") + F.pstr(""))
            assert struct.unpack("<I", p)[0] == r["have"]       # hub kept the partial upload
            res = await c2.save("big.rtf", data)
            assert h.oc.files["Documents/big.rtf"][0] == data and res["size"] == len(data)
            # wrong offset is refused and reports `have`
            await c2.request(F.CH_DOCS, F.OP_SAVE, bytes([0, 0]) + struct.pack("<II", 10, 1234) + F.pstr("c.rtf") + F.pstr(""))
            with pytest.raises(LinkError) as e:
                await c2.request(F.CH_DOCS, F.OP_WRITE, struct.pack("<I", 5) + b"xx")
            assert e.value.code == F.E_BAD_ARG and e.value.extra == struct.pack("<I", 0)
            await c2.request(F.CH_DOCS, F.OP_WRITE, struct.pack("<I", 0) + b"0123456789")
            with pytest.raises(LinkError) as e:           # CRC announced was 1234: wrong
                await c2.request(F.CH_DOCS, F.OP_COMMIT)
            assert e.value.code == F.E_BAD_CHECKSUM
            assert "Documents/c.rtf" not in h.oc.files
            await c2.close()
    run(t())


def test_size_limits():
    async def t():
        async with Hub() as h:
            h.oc.add("Documents/huge.rtf", bytes(2 << 20))
            c = await h.client()
            with pytest.raises(LinkError) as e:
                await c.open("huge.rtf")
            assert e.value.code == F.E_TOO_LARGE
            with pytest.raises(LinkError) as e:
                await c.request(F.CH_DOCS, F.OP_SAVE, bytes([0, 0]) + struct.pack("<II", 2 << 20, 0)
                                + F.pstr("h.rtf") + F.pstr(""))
            assert e.value.code == F.E_TOO_LARGE
            await c.close()
    run(t())


def test_etag_header_injection_refused():
    async def t():
        async with Hub() as h:
            c = await h.client()
            for bad in ('"x"\r\nX-Evil: 1', "a b", "x" * 80):
                with pytest.raises(LinkError) as e:
                    await c.save("e.rtf", b"x", mode=1, etag=bad)
                assert e.value.code == F.E_BAD_ARG
            assert await c.noop(b"ok") == b"ok"
            await c.close()
    run(t())


# ------------------------------------------------------------------ office formats (docx / xlsx / csv)

def _office_files():
    from test_officeconv import WP_SAMPLE, make_docx, r, real_xlsx, wp_rtf
    return wp_rtf(WP_SAMPLE), make_docx("<w:p>" + r("Hello €") + "</w:p>"), real_xlsx()


def test_list_office_types():
    async def t():
        async with Hub() as h:
            _, docx, xlsx = _office_files()
            h.oc.add("Documents/a.docx", docx)
            h.oc.add("Documents/b.XLSX", xlsx)
            h.oc.add("Documents/c.csv", b"1,2\n")
            h.oc.add("Documents/d.doc", b"x")
            c = await h.client()
            _, items = await c.list()
            by = {e.name: e for e in items}
            assert [by[n].type for n in ("a.docx", "b.XLSX", "c.csv", "d.doc")] == [3, 4, 5, 0]
            assert not any(by[n].flags & FL_UNSUPPORTED for n in ("a.docx", "b.XLSX", "c.csv"))
            await c.close()
    run(t())


def test_open_office_conversions():
    import io
    import zipfile
    from x16hub.link import sheetconv
    async def t():
        async with Hub() as h:
            _, docx, xlsx = _office_files()
            h.oc.add("Documents/a.docx", docx)
            h.oc.add("Documents/b.xlsx", xlsx)
            h.oc.add("Documents/c.csv", "x;ü\n1,5;2\n".encode())
            h.oc.add("Documents/bad.xlsx", b"PK\x03\x04 this is not a zip")
            c = await h.client()
            info, data = await c.get("a.docx", conv=1)
            assert info["type"] == 3 and not info["flags"] & FL_LOSSY
            assert data.startswith(b"{\\rtf1") and b"Hello \\'a4\\par" in data
            info, data = await c.get("a.docx", conv=0)
            assert data == docx
            info, data = await c.get("b.xlsx", conv=2)
            assert info["type"] == 4 and info["flags"] & FL_LOSSY
            z = zipfile.ZipFile(io.BytesIO(data))
            assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
            sh, _ = sheetconv.read_sheet(data)
            assert sh.cells[(1, 1)].v == "Header" and sh.cells[(4, 2)].f == "SUM($A$1:B3)+C$2*Other!B1"
            info, data = await c.get("b.xlsx", conv=0)
            assert data == xlsx
            info, data = await c.get("c.csv", conv=2)
            sh, _ = sheetconv.read_sheet(data)
            assert info["type"] == 5 and sh.cells[(2, 1)].v == "1,5" and sh.cells[(1, 2)].v == "ü"
            info, data = await c.get("c.csv", conv=0)
            assert data == "x;ü\n1,5;2\n".encode("iso8859_15")
            for name, conv in (("b.xlsx", 1), ("a.docx", 2), ("c.csv", 1)):
                with pytest.raises(LinkError) as e:
                    await c.open(name, conv)
                assert e.value.code == F.E_UNSUPPORTED, (name, conv)
            h.oc.add("Documents/x.rtf", b"{\\rtf1 x}")
            for name in ("x.rtf", "missing.txt"):
                with pytest.raises(LinkError) as e:
                    await c.open(name, 2)
                assert e.value.code == F.E_UNSUPPORTED
            with pytest.raises(LinkError) as e:
                await c.open("a.docx", 4)                   # conv 4 (BMX) is for pictures only
            assert e.value.code == F.E_UNSUPPORTED
            with pytest.raises(LinkError) as e:
                await c.open("a.docx", 5)
            assert e.value.code == F.E_BAD_ARG
            with pytest.raises(LinkError) as e:
                await c.open("bad.xlsx", 2)
            assert e.value.code == F.E_UNSUPPORTED and "convert" in e.value.msg
            assert await c.noop(b"ok") == b"ok"
            await c.close()
    run(t())


def test_save_office_conversions():
    import io
    import zipfile
    from x16hub.link import sheetconv
    from test_officeconv import sample_sheet
    async def t():
        async with Hub() as h:
            rtf, docx, _ = _office_files()
            c = await h.client()
            res = await c.save("Letter.docx", rtf, conv=1)
            stored = h.oc.files["Documents/Letter.docx"][0]
            assert res["size"] == len(stored)
            z = zipfile.ZipFile(io.BytesIO(stored))
            assert b"<w:numPr>" in z.read("word/document.xml") and b"pageBreakBefore" in z.read("word/document.xml")
            await c.save("raw.docx", docx, conv=0)
            assert h.oc.files["Documents/raw.docx"][0] == docx
            simple = sheetconv.write_simple(sample_sheet())
            await c.save("Budget.xlsx", simple, conv=2)
            stored = h.oc.files["Documents/Budget.xlsx"][0]
            z = zipfile.ZipFile(io.BytesIO(stored))
            assert z.testzip() is None and all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())
            assert sheetconv.read_sheet(stored)[0] == sample_sheet()
            await c.save("Budget.csv", simple, conv=2)
            assert h.oc.files["Documents/Budget.csv"][0].decode().startswith("Item,12.5\r\n")
            await c.save("t.csv", "a,€\n".encode("iso8859_15"), conv=0)
            assert h.oc.files["Documents/t.csv"][0] == "a,€\n".encode()
            # conv that doesn't fit the type: refused before any data is sent
            for name, conv in (("x.rtf", 2), ("x.xlsx", 1), ("x.csv", 1), ("x.docx", 2)):
                with pytest.raises(LinkError) as e:
                    await c.save(name, b"data", conv=conv)
                assert e.value.code == F.E_UNSUPPORTED, (name, conv)
            with pytest.raises(LinkError) as e:
                await c.save("x.xlsx", b"data", conv=4)
            assert e.value.code == F.E_BAD_ARG
            # bad content: E_BAD_ARG with a reason, upload dropped (a retry starts from 0)
            for name, conv, bad in (("bad.xlsx", 2, b"PK not a zip"), ("bad.docx", 1, b"plain text"),
                                    ("bad.csv", 2, b"{\\rtf1 nope}")):
                with pytest.raises(LinkError) as e:
                    await c.save(name, bad, conv=conv)
                assert e.value.code == F.E_BAD_ARG and e.value.msg, name
                p = await c.request(F.CH_DOCS, F.OP_SAVE, bytes([0, conv]) + struct.pack("<II", len(bad), F.crc32(bad))
                                    + F.pstr(name) + F.pstr(""))
                assert struct.unpack("<I", p)[0] == 0
                assert f"Documents/{name}" not in h.oc.files
            await c.close()
    run(t())
