"""`python -m x16hub oc check`: end-to-end self-test of WebDAV + CalDAV. Cleans up after itself."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .backend import owncloud as oc

TZ = ZoneInfo("America/New_York")


class Runner:
    def __init__(self, out=print):
        self.out, self.failed, self.results = out, 0, []

    def step(self, name, fn):
        try:
            detail = fn()
            ok = True
        except Exception as e:  # noqa: BLE001 -- report and keep going
            detail, ok = f"{type(e).__name__}: {oc.redact(str(e))}", False
        self.failed += not ok
        line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else "")
        self.results.append((name, ok))
        self.out(line)
        return ok


def run_check(client: oc.OwnCloud | None = None, out=print) -> int:
    cfg = None
    R = Runner(out)
    st: dict = {}

    def load():
        nonlocal cfg, client
        if client is None:
            cfg = oc.load_config()
            client = oc.OwnCloud(cfg)
        return f"{client.cfg.username} @ {client.cfg.base_url} (CA {os.path.basename(client.cfg.ca_file)})"

    if not R.step("config: creds file private, TLS CA present", load):
        return 1

    # ------------------------------------------------------------ WebDAV
    name = f"{oc.DOCS_FOLDER}/_selftest.txt"
    payload = ("x16hub self-test \u00e9\u00e8 \u2603 " + uuid.uuid4().hex + "\n").encode() + bytes(range(256))

    def folder():
        return "created X16/" if client.ensure_folder() else "X16/ exists"

    def upload():
        st["etag"] = client.put(name, payload)
        if not st["etag"]:
            raise oc.OCError("no ETag returned")
        return f"{len(payload)} bytes, etag {st['etag']}"

    def listing():
        items = {i["path"]: i for i in client.list(oc.DOCS_FOLDER)}
        it = items.get(name)
        if not it:
            raise oc.OCError(f"{name} missing from PROPFIND listing")
        if it["etag"] != st["etag"] or it["size"] != len(payload) or not it["last_modified"]:
            raise oc.OCError(f"listing mismatch: {it}")
        return f"etag+size match, lastmodified {it['last_modified']}"

    def download():
        data, etag = client.get(name)
        if data != payload:
            raise oc.OCError(f"byte mismatch ({len(data)} vs {len(payload)} bytes)")
        return f"{len(data)} bytes identical"

    def stale():
        new = client.put(name, payload + b"v2", if_match=st["etag"])   # good etag -> accepted
        if new == st["etag"]:
            raise oc.OCError("etag unchanged after overwrite")
        try:
            client.put(name, payload + b"v3", if_match=st["etag"])     # now stale -> must 412
        except oc.PreconditionFailed:
            st["etag"] = new
            return "If-Match current etag -> 2xx; stale etag -> 412 Precondition Failed"
        raise oc.OCError("PUT with stale etag was accepted (no conflict detection!)")

    def wdelete():
        client.delete(name, if_match=st["etag"])
        if client.stat(name) is not None:
            raise oc.OCError("still present after DELETE")
        return "deleted, PROPFIND -> 404"

    dav_ok = R.step("webdav: X16/ folder (MKCOL if missing)", folder)
    if dav_ok and R.step("webdav: PUT X16/_selftest.txt", upload):
        R.step("webdav: PROPFIND list (etag, getlastmodified)", listing)
        R.step("webdav: GET + byte-compare", download)
        R.step("webdav: PUT If-Match (stale etag -> 412)", stale)
        if not R.step("webdav: DELETE", wdelete):
            _cleanup(lambda: client.delete(name))

    # ------------------------------------------------------------ CalDAV
    uid = f"x16hub-selftest-{uuid.uuid4().hex}"
    start = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    title, title2 = "Caf\u00e9 test \u2615", "Caf\u00e9 test \u2615 (updated \u00fc\u00df)"

    def discover():
        cals = client.calendars()
        cal, created = client.ensure_calendar()
        st["cal"] = cal
        names = ", ".join(c["name"] for c in cals) or "-"
        return f"found [{names}]; x16 {'created' if created else 'exists'} (ctag {cal['ctag']})"

    def support():
        cal = st["cal"]
        sup = "sync-collection" in cal["reports"]
        st["sync"] = sup
        if sup:
            st["token0"] = client.sync_collection(cal, None)["token"]
        return (f"supported-report-set: {', '.join(cal['reports'])}; sync-collection "
                f"{'SUPPORTED, token ' + str(st.get('token0')) if sup else 'NOT supported (using ctag)'}")

    def create():
        st["ctag0"] = client.ctag(st["cal"])
        ics = oc.build_event(uid, title, start, start + timedelta(hours=1))
        st["href"], st["eetag"] = client.create_event(st["cal"], ics, uid)
        return f"{st['href'].rsplit('/', 1)[-1]} {start.isoformat()} {start.tzinfo}"

    def readback():
        evs = client.events(st["cal"], start - timedelta(hours=1), start + timedelta(hours=2))
        ev = next((e for e in evs if e["uid"] == uid), None)
        if not ev:
            raise oc.OCError("event not returned by calendar-query time-range")
        if ev["summary"] != title:
            raise oc.OCError(f"title mangled: {ev['summary']!r}")
        if str(getattr(ev["start"], "tzinfo", None)) != "America/New_York" or ev["start"] != start:
            raise oc.OCError(f"start/tz mismatch: {ev['start']!r}")
        st["eetag"] = ev["etag"]
        return f"title {ev['summary']!r}, start {ev['start'].isoformat()} [{ev['start'].tzinfo}]"

    def update():
        ics = oc.build_event(uid, title2, start + timedelta(minutes=30), start + timedelta(hours=2))
        st["ctag1"] = client.ctag(st["cal"])
        new = client.update_event(st["href"], ics, st["eetag"])
        try:
            client.update_event(st["href"], ics, st["eetag"])  # stale event etag must 412
            raise oc.OCError("stale event etag accepted")
        except oc.PreconditionFailed:
            pass
        ev = client.get_event(st["href"])
        if ev["summary"] != title2:
            raise oc.OCError(f"update not visible: {ev['summary']!r}")
        st["eetag"] = ev["etag"] or new
        return f"title -> {ev['summary']!r}; stale etag -> 412"

    def detect():
        ctag2 = client.ctag(st["cal"])
        if not (st["ctag0"] != st["ctag1"] != ctag2):
            raise oc.OCError(f"ctag did not change: {st['ctag0']} / {st['ctag1']} / {ctag2}")
        msg = f"ctag {st['ctag0']} -> {st['ctag1']} -> {ctag2}"
        if st.get("sync"):
            r = client.sync_collection(st["cal"], st["token0"])
            hrefs = [c["href"] for c in r["changed"]]
            if st["href"] not in hrefs:
                raise oc.OCError(f"sync-collection did not report the event: {hrefs}")
            st["token1"] = r["token"]
            msg += f"; sync-collection {st['token0']} -> {r['token']} reports {len(hrefs)} changed"
        return msg

    def cdelete():
        client.delete_event(st["href"], st["eetag"])
        try:
            client.get_event(st["href"])
            raise oc.OCError("event still present after DELETE")
        except oc.NotFound:
            pass
        msg = "deleted, GET -> 404"
        if st.get("sync"):
            r = client.sync_collection(st["cal"], st["token1"])
            if st["href"] not in r["deleted"]:
                raise oc.OCError(f"sync-collection did not report the delete: {r}")
            msg += "; sync-collection reports it deleted (404)"
        return msg

    if R.step("caldav: discover calendars + ensure 'x16'", discover):
        R.step("caldav: sync-collection (RFC 6578) support", support)
        if R.step("caldav: create event (America/New_York, non-ASCII title)", create):
            R.step("caldav: read back (time-range REPORT)", readback)
            R.step("caldav: update event (If-Match)", update)
            R.step("caldav: change detected via ctag/sync-token", detect)
            if not R.step("caldav: delete event", cdelete):
                _cleanup(lambda: client.delete_event(st["href"]))

    total = len(R.results)
    out(f"{'PASS' if not R.failed else 'FAIL'}  oc check: {total - R.failed}/{total} steps passed")
    return 1 if R.failed else 0


def _cleanup(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass
