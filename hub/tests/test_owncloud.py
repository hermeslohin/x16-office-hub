"""Unit tests for x16hub.backend.owncloud helpers (no network)."""
import json
import logging
import os
import stat
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from x16hub.backend import owncloud as oc
from x16hub.store import HubError

PW = "Sup3r-Secr3t-AppPw-XyZ"  # fake; must never surface anywhere


@pytest.fixture
def creds(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir(mode=0o700)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    f = d / "owncloud.json"
    f.write_text(json.dumps({"base_url": "https://oc.example.lan/", "username": "x16hub",
                             "app_password": PW, "ca_file": str(ca)}))
    f.chmod(0o600)
    return f


# ----------------------------------------------------------------- config

def test_load_config_ok_and_repr_hides_password(creds):
    cfg = oc.load_config(creds)
    assert cfg.base_url == "https://oc.example.lan"
    assert PW not in repr(cfg) and PW not in str(cfg)
    assert PW not in repr(oc.OwnCloud(cfg))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_load_config_refuses_group_or_world_readable(creds, mode):
    creds.chmod(mode)
    with pytest.raises(HubError, match="chmod 600"):
        oc.load_config(creds)


def test_load_config_refuses_open_directory(creds):
    creds.parent.chmod(0o755)
    with pytest.raises(HubError, match="chmod 700"):
        oc.load_config(creds)


@pytest.mark.parametrize("url", ["http://oc.example.lan", "https://u:p@oc.example.lan", "ftp://x", "https://"])
def test_load_config_requires_plain_https(creds, url):
    raw = json.loads(creds.read_text())
    raw["base_url"] = url
    creds.write_text(json.dumps(raw))
    with pytest.raises(HubError, match="https"):
        oc.load_config(creds)


def test_load_config_missing_key(creds):
    raw = json.loads(creds.read_text())
    del raw["ca_file"]
    creds.write_text(json.dumps(raw))
    with pytest.raises(HubError, match="ca_file"):
        oc.load_config(creds)


def test_bad_json_error_does_not_leak(creds):
    creds.write_text('{"app_password": "' + PW + '", oops')
    with pytest.raises(HubError) as ei:
        oc.load_config(creds)
    assert PW not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


# ----------------------------------------------------------------- helpers

def test_join_path_encodes_and_blocks_traversal():
    assert oc.join_path("X16", "Café notes.txt") == "X16/Caf%C3%A9%20notes.txt"
    assert oc.join_path("X16//a/./b") == "X16/a/b"
    assert oc.join_path("a#b?c") == "a%23b%3Fc"
    with pytest.raises(oc.OCError):
        oc.join_path("X16/../../admin")


@pytest.mark.parametrize("raw,want", [('"abc"', '"abc"'), ("abc", '"abc"'), ('W/"abc"', '"abc"'),
                                      (None, None), ("", None)])
def test_norm_etag(raw, want):
    assert oc.norm_etag(raw) == want


def test_next_mtime_strictly_after_previous():
    lm = "Tue, 17 Nov 2026 15:35:07 GMT"
    prev = int(datetime(2026, 11, 17, 15, 35, 7, tzinfo=ZoneInfo("UTC")).timestamp())
    assert oc.next_mtime(lm, now=prev) == prev + 1          # same second -> bump
    assert oc.next_mtime(lm, now=prev + 50) == prev + 50    # clock ahead -> now
    assert oc.next_mtime(None, now=123) == 123
    assert oc.next_mtime("garbage", now=123) == 123


MS = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
 <d:response><d:href>/remote.php/dav/files/x16hub/X16/</d:href>
  <d:propstat><d:prop><d:getetag>"e1"</d:getetag><d:resourcetype><d:collection/></d:resourcetype></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  <d:propstat><d:prop><d:getcontentlength/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
 </d:response>
 <d:response><d:href>/remote.php/dav/files/x16hub/X16/Caf%C3%A9.txt</d:href>
  <d:propstat><d:prop><d:getetag>"e2"</d:getetag><d:getcontentlength>5</d:getcontentlength>
   <d:getlastmodified>Tue, 17 Nov 2026 15:35:07 GMT</d:getlastmodified><d:resourcetype/></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
</d:multistatus>"""


def test_parse_multistatus():
    items = oc.parse_multistatus(MS)
    assert [i["href"] for i in items] == ["/remote.php/dav/files/x16hub/X16/",
                                         "/remote.php/dav/files/x16hub/X16/Café.txt"]
    assert "{DAV:}getcontentlength" not in items[0]["props"]  # 404 propstat dropped
    assert items[1]["props"]["{DAV:}getcontentlength"].text == "5"


def test_parse_multistatus_rejects_xxe():
    evil = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>
    <d:multistatus xmlns:d="DAV:"><d:response><d:href>&e;</d:href></d:response></d:multistatus>"""
    with pytest.raises(Exception) as ei:
        oc.parse_multistatus(evil)
    assert "root:" not in str(ei.value)


def test_event_roundtrip_ny_non_ascii():
    tz = ZoneInfo("America/New_York")
    start = datetime(2026, 7, 4, 9, 30, tzinfo=tz)
    ics = oc.build_event("u-1", "Café test ☕ üß", start, start + timedelta(hours=1))
    assert b"BEGIN:VTIMEZONE" in ics and b"TZID=America/New_York" in ics
    ev = oc.parse_event(ics)
    assert ev["uid"] == "u-1" and ev["summary"] == "Café test ☕ üß"
    assert ev["start"] == start and str(ev["start"].tzinfo) == "America/New_York"


# ----------------------------------------------------------------- client with a fake transport

def FakeResp(status=200, body=b"", headers=None):
    """A real requests.Response streaming from memory (exercises the bounded body read)."""
    import io
    from requests.structures import CaseInsensitiveDict
    r = requests.Response()
    r.status_code, r.headers, r.raw = status, CaseInsensitiveDict(headers or {}), io.BytesIO(body)
    r.encoding = "utf-8"
    return r


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(creds, responses):
    return oc.OwnCloud(oc.load_config(creds), session=FakeSession(responses))


def test_session_always_verifies_with_ca(creds):
    c = client(creds, [])
    assert c.s.verify == oc.load_config(creds).ca_file
    assert c.s.trust_env is False and c.s.max_redirects == 0


def test_put_if_match_412_raises_conflict(creds):
    c = client(creds, [FakeResp(207, MS.replace(b"X16/</d:href>", b"X16/a.txt</d:href>")),
                       FakeResp(412, b"<s:message>An If-Match header was specified</s:message>")])
    with pytest.raises(oc.PreconditionFailed) as ei:
        c.put("X16/a.txt", b"data", if_match='"old"')
    method, url, kw = c.s.calls[1]
    assert method == "PUT" and kw["headers"]["If-Match"] == '"old"'
    assert "X-OC-MTime" in kw["headers"] and ei.value.status == 412


def test_put_rejects_unchanged_etag(creds):
    c = client(creds, [FakeResp(404), FakeResp(204, headers={"ETag": '"same"'})])
    with pytest.raises(oc.OCError, match="kept the old etag"):
        c.put("X16/a.txt", b"x", if_match='"same"')


def test_refuses_requests_outside_base_url(creds):
    c = client(creds, [])
    with pytest.raises(oc.OCError, match="outside base_url"):
        c._req("GET", "https://evil.example/", ok=(200,))
    with pytest.raises(oc.OCError, match="outside base_url"):
        c._req("GET", "https://oc.example.lan.evil.example/x", ok=(200,))
    assert c.s.calls == []


def test_sync_collection_parsing(creds):
    body = b"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
     <d:response><d:href>/remote.php/dav/calendars/x16hub/x16/a.ics</d:href>
      <d:propstat><d:prop><d:getetag>"1"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
     <d:response><d:href>/remote.php/dav/calendars/x16hub/x16/b.ics</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>
     <d:sync-token>http://sabre.io/ns/sync/7</d:sync-token></d:multistatus>"""
    c = client(creds, [FakeResp(207, body)])
    r = c.sync_collection({"href": "/remote.php/dav/calendars/x16hub/x16/"}, "http://sabre.io/ns/sync/5")
    assert r == {"token": "http://sabre.io/ns/sync/7",
                 "changed": [{"href": "/remote.php/dav/calendars/x16hub/x16/a.ics", "etag": '"1"'}],
                 "deleted": ["/remote.php/dav/calendars/x16hub/x16/b.ics"]}
    assert b"<d:sync-token>http://sabre.io/ns/sync/5</d:sync-token>" in c.s.calls[0][2]["data"]


def test_create_event_rejects_unsafe_uid(creds):
    c = client(creds, [])
    with pytest.raises(oc.OCError):
        c.create_event({"href": "/remote.php/dav/calendars/x16hub/x16/"}, b"", uid="../../x")


# ----------------------------------------------------------------- password never leaks

def test_password_not_in_transport_exception(creds):
    boom = requests.ConnectionError(f"failed for https://x16hub:{PW}@oc.example.lan Authorization: Basic eDE2aHViOnB3")
    c = client(creds, [boom])
    with pytest.raises(oc.OCError) as ei:
        c.get("X16/a.txt")
    text = str(ei.value) + repr(ei.value)
    assert PW not in text and "eDE2aHViOnB3" not in text and "***" in text
    assert ei.value.__cause__ is None  # original exception (with secret) not chained


def test_password_not_in_http_error(creds):
    c = client(creds, [FakeResp(500, f"<s:message>bad {PW}</s:message>".encode())])
    with pytest.raises(oc.OCError) as ei:
        c.get("X16/a.txt")
    assert PW not in str(ei.value)


def test_tls_failure_message_is_clean(creds):
    c = client(creds, [requests.exceptions.SSLError(f"cert verify failed {PW}")])
    with pytest.raises(oc.OCError, match="TLS verification failed") as ei:
        c.get("X16/a.txt")
    assert PW not in str(ei.value)


def test_password_never_in_log_output(creds, caplog):
    oc.load_config(creds)  # registers the secret
    lg = logging.getLogger("x16hub.test")
    handler = logging.StreamHandler(stream := __import__("io").StringIO())
    lg.addHandler(handler)
    oc.install_log_redaction(lg)
    try:
        lg.warning("auth=%s", PW)
        lg.error("header Authorization: Basic abcdEFGH==")
        try:
            raise RuntimeError(f"inner {PW}")
        except RuntimeError:
            lg.exception("failed")
    finally:
        lg.removeHandler(handler)
    out = stream.getvalue() + caplog.text
    assert PW not in out and "abcdEFGH" not in out
    assert out.count("***") >= 3


def test_selftest_output_never_contains_password(creds, monkeypatch):
    from x16hub import selftest
    monkeypatch.setenv("X16HUB_OC_CREDS", str(creds))
    boom = requests.ConnectionError(f"boom {PW}")
    c = client(creds, [boom] * 50)
    lines = []
    rc = selftest.run_check(client=c, out=lines.append)
    assert rc == 1 and any(l.startswith("FAIL") for l in lines)
    assert all(PW not in l for l in lines)


def test_cli_refuses_world_readable_creds(creds, monkeypatch, capsys):
    from x16hub.__main__ import main
    creds.chmod(0o644)
    monkeypatch.setenv("X16HUB_OC_CREDS", str(creds))
    rc = main(["oc", "check"])
    out = capsys.readouterr()
    assert rc == 1
    assert "chmod 600" in out.out + out.err
    assert PW not in out.out + out.err
