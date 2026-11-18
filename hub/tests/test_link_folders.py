"""Docs channel folder ops (DIR / MKDIR / DELETE / MOVE / COPY) and raw transfers (conv 3)."""
import pytest

from test_link_server import Hub, run
from x16hub.link import frame as F
from x16hub.link.client import LinkError
from x16hub.link.docs import FL_UNSUPPORTED, T_DIR


async def err(coro):
    with pytest.raises(LinkError) as e:
        await coro
    return e.value


def test_dir_lists_one_folder_folders_first():
    async def t():
        async with Hub() as h:
            h.oc.add("Documents/b.rtf", b"x")
            h.oc.add("Documents/A.PRG", b"\x01\x08")
            h.oc.add("Documents/zeta/in.txt", b"t")
            h.oc.add("Documents/Alpha/deep/x.csv", b"c")
            c = await h.client()
            total, es = await c.dir("")
            assert total == 4
            assert [(e.type, e.name) for e in es] == [(T_DIR, "Alpha"), (T_DIR, "zeta"), (0, "A.PRG"), (1, "b.rtf")]
            # in a DIR listing, any type can be transferred raw: type 0 is not "unsupported"
            assert not es[2].flags & FL_UNSUPPORTED
            total, es = await c.dir("Alpha")
            assert [(e.type, e.name) for e in es] == [(T_DIR, "deep")]
            total, es = await c.dir("Alpha/deep")
            assert [(e.type, e.name, e.size) for e in es] == [(5, "x.csv", 1)]
            assert (await err(c.dir("nope"))).code == F.E_NOT_FOUND
            assert (await err(c.dir("b.rtf"))).code == F.E_NOT_FOUND
            assert (await err(c.dir("../x"))).code == F.E_BAD_NAME
            await c.close()
    run(t())


def test_dir_paging_small_maxrx():
    async def t():
        async with Hub() as h:
            for i in range(40):
                h.oc.add(f"Documents/f/file-number-{i:03d}.txt", b"x" * i)
            c = await h.client(maxrx=128)
            total, es = await c.dir("f", page=5)
            assert total == 40 and [e.name for e in es] == [f"file-number-{i:03d}.txt" for i in range(40)]
            await c.close()
    run(t())


def test_mkdir_delete():
    async def t():
        async with Hub() as h:
            c = await h.client()
            await c.mkdir("Letters")
            await c.mkdir("Letters/2026")
            assert "Documents/Letters/2026" in h.oc.dirs
            e = await err(c.mkdir("Letters"))
            assert e.code == F.E_CONFLICT and e.extra[0] == F.CONFLICT_EXISTS
            assert (await err(c.mkdir("Missing/sub"))).code == F.E_BAD_NAME     # parent folder missing
            for bad in ("", "a/../b", ".hidden", "a:b", "x" * 64):
                assert (await err(c.mkdir(bad))).code == F.E_BAD_NAME, bad
            h.oc.add("Documents/Letters/2026/mom.rtf", b"r")
            await c.delete("Letters")                       # folders go with their contents
            assert not any(k.startswith("Documents/Letters") for k in h.oc.files)
            assert "Documents/Letters" not in h.oc.dirs and "Documents" in h.oc.dirs
            assert (await err(c.delete("Letters"))).code == F.E_NOT_FOUND
            assert (await err(c.delete(""))).code == F.E_BAD_NAME           # never Documents/ itself
            assert (await err(c.delete(".."))).code == F.E_BAD_NAME
            await c.close()
    run(t())


def test_move_copy():
    async def t():
        async with Hub() as h:
            h.oc.add("Documents/a.rtf", b"A")
            h.oc.add("Documents/b.rtf", b"B")
            h.oc.add("Documents/d/x.txt", b"X")
            c = await h.client()
            await c.copy("a.rtf", "d/a2.rtf")
            assert h.oc.files["Documents/d/a2.rtf"][0] == b"A" and "Documents/a.rtf" in h.oc.files
            await c.move("a.rtf", "d/a.rtf")
            assert "Documents/a.rtf" not in h.oc.files and h.oc.files["Documents/d/a.rtf"][0] == b"A"
            # destination exists: CONFLICT unless mode 1
            e = await err(c.move("b.rtf", "d/a.rtf"))
            assert e.code == F.E_CONFLICT and e.extra[0] == F.CONFLICT_EXISTS
            await c.move("b.rtf", "d/a.rtf", mode=1)
            assert h.oc.files["Documents/d/a.rtf"][0] == b"B"
            # folders: copy and move whole trees, never into themselves, never file over folder
            await c.copy("d", "e")
            assert h.oc.files["Documents/e/x.txt"][0] == b"X" and "Documents/d/x.txt" in h.oc.files
            assert (await err(c.move("d", "d/sub"))).code == F.E_BAD_ARG
            assert (await err(c.copy("d", "d"))).code == F.E_BAD_ARG
            h.oc.add("Documents/f.txt", b"F")
            assert (await err(c.move("f.txt", "e", mode=1))).code == F.E_CONFLICT
            await c.mkdir("g")
            await c.move("d", "g/d")
            assert h.oc.files["Documents/g/d/x.txt"][0] == b"X" and "Documents/d" not in h.oc.dirs
            assert (await err(c.move("nope", "x"))).code == F.E_NOT_FOUND
            assert (await err(c.copy("f.txt", "nofolder/f.txt"))).code == F.E_BAD_NAME
            assert (await err(c.copy("f.txt", "../f.txt"))).code == F.E_BAD_NAME
            assert (await err(c.request(F.CH_DOCS, F.OP_MOVE, bytes([2, 1]) + b"f" + bytes([1]) + b"g"))).code == F.E_BAD_ARG
            await c.close()
    run(t())


def test_raw_conv_any_type_byte_exact():
    async def t():
        async with Hub() as h:
            blob = bytes(range(256)) * 3
            c = await h.client()
            await c.mkdir("bin")
            r = await c.save("bin/GAME.PRG", blob, conv=3)
            assert r["size"] == len(blob) and h.oc.files["Documents/bin/GAME.PRG"][0] == blob
            info, data = await c.get("bin/GAME.PRG", conv=3)
            assert data == blob and info["type"] == 0
            # raw keeps text bytes as they are (no UTF-8 conversion)
            await c.save("t.txt", "é\n".encode("iso8859_15"), conv=3)
            assert h.oc.files["Documents/t.txt"][0] == b"\xe9\n"
            h.oc.add("Documents/u.txt", "é".encode())
            assert (await c.get("u.txt", conv=3))[1] == "é".encode()
            # without conv 3 the old rules still hold
            assert (await err(c.save("x.prg", b"x", conv=0))).code == F.E_BAD_NAME
            assert (await err(c.open("bin/GAME.PRG", 0))).code == F.E_UNSUPPORTED
            # raw names still follow the name rules
            assert (await err(c.save("../x.prg", b"x", conv=3))).code == F.E_BAD_NAME
            await c.close()
    run(t())
