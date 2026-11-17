"""Regression tests for the W7 security review findings (F1-F5, hub side)."""
import asyncio
import os
import stat

import pytest

from test_link_server import DEV, KEY, Hub, closed, run
from test_owncloud import FakeResp, client, creds  # noqa: F401 (fixture)
from x16hub.backend import owncloud as oc
from x16hub.link import frame as F
from x16hub.link import keys as K
from x16hub.link.client import LinkError
from x16hub.link.server import LinkServer
from x16hub.store import HubError


# ---- F1 (+ N1): idle pre-auth connections can't lock the real X16 out
def test_f1_idle_preauth_conns_are_evicted_and_cost_a_failure():
    async def t():
        async with Hub(evict_after=0.2, hello=5) as h:
            idle = [await h.raw() for _ in range(2)]            # never send HELLO
            await asyncio.sleep(0.3)
            h.srv.limits.max_conns = 2
            h.srv.limits.max_conns_per_ip = 3
            c = await h.client()                                # evicts the oldest idle one
            assert (await c.noop(b"x")) == b"x"
            await c.close()
            assert await closed(idle[0][0], 1)
            assert len(h.srv.fails.get("127.0.0.1", [])) == 1   # squatting costs a failure (N1)
            for r, w in idle:
                w.close()
    run(t())


def test_n1_mid_handshake_session_is_never_evicted():
    """A client that sent HELLO and is still computing its HMAC keeps its slot, however many
    newcomers arrive; the newcomers are refused instead."""
    async def t():
        async with Hub(evict_after=0.1, hello=5, handshake=10) as h:
            h.srv.limits.max_conns, h.srv.limits.max_conns_per_ip = 1, 5
            r, w = await h.raw()
            w.write(F.encode(0, 1, F.OP_HELLO, F.MAGIC + bytes([1]) + (512).to_bytes(2, "little")
                             + F.pstr(DEV.encode()) + bytes(16)))
            await w.drain()
            hello = await asyncio.wait_for(r.read(64), 2)
            assert hello                                        # got HELLO'
            await asyncio.sleep(0.3)
            for _ in range(3):
                r2, w2 = await h.raw()
                assert await closed(r2, 1)                      # newcomer refused, not us
                w2.close()
            assert any(s.hello_ok for s in h.srv.sessions)     # still there, mid-handshake
            w.close()
    run(t())


def test_f1_handshake_timeout_counts_as_auth_failure():
    async def t():
        async with Hub(hello=0.2, auth_fail_max=2) as h:
            for _ in range(2):
                r, w = await h.raw()
                assert await closed(r, 2)
                w.close()
            assert h.srv.is_blocked("127.0.0.1")
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await h.client()
    run(t())


def test_f1_preauth_garbage_counts_as_auth_failure():
    async def t():
        async with Hub(auth_fail_max=3) as h:
            for _ in range(3):
                r, w = await h.raw()
                w.write(b"\x05\x00\x00\x01\x03hello\x00\x00")         # bad CRC before auth
                await w.drain()
                assert await closed(r, 2)
                w.close()
            assert h.srv.is_blocked("127.0.0.1")
    run(t())


# ---- F2: IPv6 peers are counted per /64
@pytest.mark.parametrize("ip,key", [
    ("2001:db8:1:2::5", "2001:db8:1:2::/64"), ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
    ("fe80::1%eth0", "fe80::/64"), ("::ffff:10.0.0.7", "10.0.0.7"), ("10.0.0.7", "10.0.0.7")])
def test_f2_peer_key(ip, key):
    assert LinkServer.peer_key(ip) == key


def test_f2_ipv6_address_rotation_shares_one_lockout():
    s = LinkServer(keys_loader=dict)
    for i in range(4 * s.limits.auth_fail_max):               # 20 different addresses, same /64
        s.auth_failed(f"fd00:1:2:3::{i + 1:x}")
    assert s.is_blocked("fd00:1:2:3::ffff") and not s.is_blocked("fd00:1:2:4::1")


def test_n4_one_host_cannot_lock_out_its_64_neighbours_quickly():
    s = LinkServer(keys_loader=dict)
    for _ in range(s.limits.auth_fail_max):
        s.auth_failed("fe80::bad%eth0")
    assert s.is_blocked("fe80::bad%eth0")
    assert not s.is_blocked("fe80::1%eth0")                   # the X16 next to it still gets in
    assert LinkServer.addr_key("::ffff:10.0.0.7") == "10.0.0.7"


# ---- F3: converter/backend exceptions answer ERR and keep the session
def test_f3_unconvertible_rtf_to_txt_is_bad_arg_and_session_survives():
    async def t():
        async with Hub() as h:
            c = await h.client()
            with pytest.raises(LinkError) as e:
                await c.save("deep.txt", b"{" * 70, conv=1)
            assert e.value.code == F.E_BAD_ARG
            assert (await c.noop(b"ok")) == b"ok"
            assert not any(h.srv.pending.by_dev.values())     # dropped: retrying cannot help
            await c.close()
    run(t())


def test_f3_unexpected_handler_exception_is_err_backend():
    async def t():
        async with Hub() as h:
            def boom(*a, **k):
                raise KeyError("odd server data")
            h.oc.list = boom
            h.oc.stat = boom
            c = await h.client()
            with pytest.raises(LinkError) as e:
                await c.list()
            assert e.value.code == F.E_BACKEND and "internal" in e.value.msg
            assert (await c.noop(b"ok")) == b"ok"
            await c.close()
    run(t())


def test_f3_bad_xml_and_bad_contentlength_become_ocerror(creds):  # noqa: F811
    with pytest.raises(oc.OCError):
        oc.parse_multistatus(b"<not xml")
    ms = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/remote.php/dav/files/'
          b'x16hub/a.txt</d:href><d:propstat><d:prop><d:getcontentlength>12abc</d:getcontentlength></d:prop>'
          b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>')
    c = client(creds, [FakeResp(207, ms)])
    assert c.list("a.txt", depth=0)[0]["size"] == 0


# ---- F4: key files never follow planted symlinks / reuse planted files
def test_f4_sd_keyfile_replaces_symlink_instead_of_following(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / K.SD_KEY_NAME).symlink_to(victim)
    out = K.write_sd_keyfile(sd, "10.0.0.2:6502", "x16", bytes(32))
    assert victim.read_text() == "precious"
    assert not out.is_symlink() and out.read_text().startswith("HOST=10.0.0.2:6502\n")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_f4_sd_keyfile_replaces_preexisting_world_writable_file(tmp_path):
    p = tmp_path / K.SD_KEY_NAME
    p.write_text("x")
    os.chmod(p, 0o666)
    ino = p.stat().st_ino
    K.write_sd_keyfile(tmp_path, "10.0.0.2:6502", "x16", bytes(32))
    assert p.stat().st_ino != ino and stat.S_IMODE(p.stat().st_mode) == 0o600


def test_f4_keys_file_refuses_open_parent_and_symlink(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    os.chmod(d, 0o777)
    with pytest.raises(HubError):
        K.new_key("x16", d / "keys")
    os.chmod(d, 0o700)
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (d / "keys").symlink_to(victim)
    with pytest.raises(HubError):          # load_keys refuses the symlink (not a private regular file)
        K.new_key("x16", d / "keys")
    assert victim.read_text() == "precious"


def test_f4_sd_keyfile_validates_devid(tmp_path):
    with pytest.raises(HubError):
        K.write_sd_keyfile(tmp_path, "10.0.0.2:6502", "x16\nKEY=00", bytes(32))


# ---- F5: response bodies are bounded while reading
def test_f5_get_aborts_when_body_exceeds_limit(creds):  # noqa: F811
    c = client(creds, [FakeResp(200, b"A" * 5000, {"ETag": '"e"'})])
    with pytest.raises(oc.TooLarge):
        c.get("x.txt", max_bytes=4096)


def test_f5_content_length_over_limit_refused_before_reading(creds):  # noqa: F811
    c = client(creds, [FakeResp(200, b"", {"Content-Length": str(10 << 30)})])
    with pytest.raises(oc.TooLarge):
        c.get("x.txt", max_bytes=1 << 20)


def test_f5_open_of_grown_file_is_too_large():
    async def t():
        async with Hub() as h:
            h.oc.put("Documents/big.txt", b"x" * 10)
            real_get = h.oc.get

            def grown(path, max_bytes=None):     # file grew between PROPFIND and GET
                data, et = real_get(path)
                data = b"x" * ((1 << 20) + 1)
                if max_bytes is not None and len(data) > max_bytes:
                    raise oc.TooLarge("too big")
                return data, et
            h.oc.get = grown
            c = await h.client()
            with pytest.raises(LinkError) as e:
                await c.open("big.txt")
            assert e.value.code == F.E_TOO_LARGE
            await c.close()
    run(t())


# ---- N2: a lost CALENDAR.DAT must not wipe the calendar
def _cal_env(tmp_path):
    import sqlite3
    from datetime import date

    from fake_caldav import FakeOC
    from test_calsync import TODAY, vevent
    from x16hub import calsync as C
    oc = FakeOC()
    cs = C.CalSync(oc, sqlite3.connect(tmp_path / "hub.db"), today=lambda: TODAY, lock_path=tmp_path / "lock")
    for i in range(12):
        oc.server_put(f"e{i}", vevent(f"e{i}", date(2026, 11, 20 + i % 5), f"Event {i}"))
    push, pull = cs.sync([(s, bytes(32)) for s in range(128)], 0)
    return C, oc, cs, pull


def test_n2_full_sync_with_empty_slab_never_deletes(tmp_path):
    C, oc, cs, pull = _cal_env(tmp_path)
    assert len(oc.objs) == 12
    push, _ = cs.sync([(s, bytes(32)) for s in range(128)], 0)     # counter lost / unknown
    assert push.deleted == 0 and len(oc.objs) == 12


def test_n2_mass_delete_refused_unless_armed(tmp_path):
    C, oc, cs, pull = _cal_env(tmp_path)
    with pytest.raises(C.MassDelete):
        cs.sync([(s, bytes(32)) for s in range(128)], pull.counter)   # empty slab, valid counter
    assert len(oc.objs) == 12
    recs = dict(pull.records)                                          # deleting a few is fine
    for s in list(k for k, v in recs.items() if v[0])[:2]:
        recs[s] = bytes(32)
    push, pull2 = cs.sync(sorted(recs.items()), pull.counter)
    assert push.deleted == 2 and len(oc.objs) == 10
    cs.allow_mass_delete()
    push, _ = cs.sync([(s, bytes(32)) for s in range(128)], pull2.counter)
    assert push.deleted == 10 and len(oc.objs) == 0


def test_n2_mass_delete_over_link_is_bad_state(tmp_path):
    from test_link_cal import Hub as CalHub, all_slots
    from datetime import date
    from test_calsync import vevent

    async def t():
        async with CalHub(tmp_path) as h:
            for i in range(12):
                h.oc.server_put(f"e{i}", vevent(f"e{i}", date(2026, 11, 20 + i % 5), f"Event {i}"))
            c = await h.client()
            r = await c.cal_sync(all_slots({}), 0)
            with pytest.raises(LinkError) as e:
                await c.cal_sync(all_slots({}), r["counter"])
            assert e.value.code == F.E_BAD_STATE and "delete 12" in e.value.msg
            assert len(h.oc.objs) == 12
            await c.close()
    run(t())


# ---- N3: the `calsync dat` CLI refuses to mix another CALENDAR.DAT into a bound X16's state
def test_n3_cli_dat_refuses_when_device_bound(tmp_path, monkeypatch, capsys):
    import sqlite3
    from x16hub import calsync as C
    monkeypatch.setenv("X16HUB_DATA", str(tmp_path / "data"))
    from x16hub.store import connect
    db = connect()
    db.execute("CREATE TABLE IF NOT EXISTS cal_meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO cal_meta VALUES ('x16_device','x16-office')")
    db.commit()
    db.close()
    monkeypatch.setattr(C, "_client", lambda: (_ for _ in ()).throw(AssertionError("must not get here")))
    assert C.main(["dat", str(tmp_path / "CALENDAR.DAT")]) == 1
    assert "belongs to X16 'x16-office'" in capsys.readouterr().err
