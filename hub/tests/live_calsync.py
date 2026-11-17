"""Live test of calsync against the real ownCloud `x16` calendar.

    cd ~/x16-office/hub && .venv/bin/python tests/live_calsync.py

* Uses a throw-away hub.db in a temp dir (the real hub/data/hub.db is not touched).
* Only touches events it creates (summary starts with W6TEST; UIDs are recorded) and
  deletes them all at the end, even when a step fails. Other events in the calendar are
  only read (they show up in the simulated X16 and are never written).
* Never prints credentials.
"""
import os
import sqlite3
import sys
import tempfile
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from icalendar import Calendar  # noqa: E402

from test_calsync import X16, PET, vevent, weekly  # noqa: E402
from x16hub import calsync as C  # noqa: E402
from x16hub.backend import owncloud as oc_mod  # noqa: E402

NY = C.NY
results = []
created_uids: set[str] = set()


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return cond


def find_server(oc, cal, prefix="W6TEST"):
    out = {}
    start = datetime.now(timezone.utc) - timedelta(days=60)
    for ev in oc.events(cal, start, start + timedelta(days=500)):
        for e in Calendar.from_ical(ev["ics"]).walk("VEVENT"):
            if str(e.get("summary", "")).startswith(prefix) and "recurrence-id" not in e:
                out[str(e["uid"])] = (ev, e)
    return out


def main():
    oc_mod.install_log_redaction()
    oc = oc_mod.OwnCloud(oc_mod.load_config())
    tmp = Path(tempfile.mkdtemp(prefix="w6live-"))
    os.chmod(tmp, 0o700)
    cs = C.CalSync(oc, sqlite3.connect(tmp / "hub.db"), lock_path=tmp / "lock")
    cal, _ = oc.ensure_calendar("x16")
    today = datetime.now(NY).date()
    d1, d2, d3, d4 = (today + timedelta(days=n) for n in (3, 4, 5, 6))
    x = X16()
    pre = len(find_server(oc, cal, ""))
    print(f"calendar {cal['href']}: {pre} existing event(s) in range (read-only for this test)")
    try:
        x.sync(cs)
        # 1. X16 creates (timed + all-day, PETSCII £) -> CalDAV
        s_timed = x.add(d1, "14:30 W6TEST dentist £5")
        s_all = x.add(d2, "W6TEST bike day")
        push, pull = x.sync(cs)
        srv = find_server(oc, cal)
        created_uids.update(srv)
        t = [e for _, e in srv.values() if str(e["summary"]) == "W6TEST dentist £5"]
        check("X16 create (timed) visible in CalDAV", len(t) == 1 and
              t[0].decoded("dtstart") == datetime.combine(d1, datetime.min.time().replace(hour=14, minute=30), tzinfo=NY),
              t and str(t[0].decoded("dtstart")))
        a = [e for _, e in srv.values() if str(e["summary"]) == "W6TEST bike day"]
        check("X16 create (all-day) visible in CalDAV", len(a) == 1 and a[0].decoded("dtstart") == d2)
        check("PETSCII £ -> UTF-8 £ on server", t and "£" in str(t[0]["summary"]))

        # 2. Edit in CalDAV -> X16
        uid_t = str(t[0]["uid"])
        ev, _ = srv[uid_t]
        calobj = Calendar.from_ical(ev["ics"])
        e = calobj.walk("VEVENT")[0]
        del e["summary"]
        e.add("summary", "W6TEST Crème brûlée ☕ Zoë")
        e.add("location", "Springfield")
        oc.update_event(ev["href"], calobj.to_ical(), ev["etag"])
        x.sync(cs)
        check("CalDAV edit (non-ASCII) reaches X16 record", x.text(s_timed) == "14:30 W6TEST Creme brulee ?",
              repr(x.text(s_timed)))

        # 3. X16 in-place edit keeps accents, emoji, location (v2 edited bit)
        x.edit(s_timed, text="14:30 W6TEST Creme BRULEE ?")
        push, _ = x.sync(cs)
        e = Calendar.from_ical(oc.get_event(ev["href"])["ics"]).walk("VEVENT")[0]
        check("X16 edit merged, Unicode + hidden fields kept",
              str(e["summary"]) == "W6TEST Crème BRULEE ☕ Zoë" and str(e["location"]) == "Springfield",
              repr(str(e["summary"])))

        # 4. Delete X16 -> server
        uid_a = str(a[0]["uid"])
        x.delete(s_all)
        push, _ = x.sync(cs)
        check("X16 delete removes CalDAV event", uid_a not in find_server(oc, cal) and push.deleted == 1)

        # 5. Delete server -> X16
        uid_s = "w6test-" + uuid.uuid4().hex[:12]
        oc.create_event(cal, vevent(uid_s, d3, "W6TEST server-born", end=d3 + timedelta(days=1)), uid_s)
        created_uids.add(uid_s)
        x.sync(cs)
        ok1 = len(x.find("W6TEST server-born")) == 1
        oc.delete_event(find_server(oc, cal)[uid_s][0]["href"])
        x.sync(cs)
        check("CalDAV create appears on X16, CalDAV delete clears X16 slot",
              ok1 and not x.find("W6TEST server-born"))

        # 6. Conflict: X16 deletes while CalDAV edits -> edit kept + flagged
        uid_c = "w6test-" + uuid.uuid4().hex[:12]
        href_c, _ = oc.create_event(cal, vevent(uid_c, d4, "W6TEST party", end=d4 + timedelta(days=1)), uid_c)
        created_uids.add(uid_c)
        x.sync(cs)
        (sc,) = x.find("W6TEST party")
        x.delete(sc)
        cur = oc.get_event(href_c)
        co = Calendar.from_ical(cur["ics"])
        co.walk("VEVENT")[0].add("location", "Bob's")
        oc.update_event(href_c, co.to_ical(), cur["etag"])
        push, pull = x.sync(cs)
        back = x.find("W6TEST party")
        check("conflict: X16 delete vs CalDAV edit keeps the event + flags it",
              push.conflicts == 1 and uid_c in find_server(oc, cal) and len(back) == 1
              and x.rec(back[0]).flags & C.F_CONFLICT and pull.conflicts[-1]["kind"] == "x16-delete-vs-server-edit")

        # 7. Recurring: delete one occurrence on X16 -> EXDATE on server
        uid_w = "w6test-" + uuid.uuid4().hex[:12]
        st = datetime.combine(today + timedelta(days=1), datetime.min.time().replace(hour=19), tzinfo=NY)
        href_w, _ = oc.create_event(cal, weekly(uid_w, st, "W6TEST weekly"), uid_w)
        created_uids.add(uid_w)
        x.sync(cs)
        occ = sorted((x.rec(s).date, s) for s in x.find("19:00 W6TEST weekly"))
        x.delete(occ[2][1])
        x.sync(cs)
        m = Calendar.from_ical(oc.get_event(href_w)["ics"]).walk("VEVENT")[0]
        exd = [d.dt for d in m["exdate"].dts] if "exdate" in m else []
        check("recurring: 10 occurrences mapped, X16 delete -> EXDATE",
              len(occ) == 10 and len(exd) == 1 and exd[0].astimezone(NY).date() == occ[2][0]
              and len(x.find("19:00 W6TEST weekly")) == 9, f"occ={len(occ)} exdate={exd}")

        # 8. Idempotency on the real server: lost replies, repeat
        n_before = len(find_server(oc, cal))
        x.add(d1, "W6TEST once")
        x.sync(cs, apply=False)
        x.sync(cs, apply=False)
        x.sync(cs)
        srv = find_server(oc, cal)
        created_uids.update(srv)
        check("lost pull replies do not duplicate on the server",
              len(srv) == n_before + 1 and len(x.find("W6TEST once")) == 1)
    except Exception:
        traceback.print_exc()
        check("no exception", False)
    finally:
        # cleanup: everything this test created, whatever happened
        left = find_server(oc, cal)
        for uid in set(left) | created_uids:
            if uid in left:
                try:
                    oc.delete_event(left[uid][0]["href"])
                except oc_mod.OCError as e:
                    print("cleanup failed for", uid, e)
        rest = find_server(oc, cal)
        check("cleanup: no W6TEST events left", not rest, ",".join(rest))
        post = len(find_server(oc, cal, ""))
        check("other events untouched (count unchanged)", post == pre, f"{pre} -> {post}")
    npass = sum(ok for _, ok in results)
    print(f"\n{npass}/{len(results)} checks passed")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
