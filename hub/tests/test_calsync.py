"""Unit tests for x16hub.calsync against an in-memory CalDAV (no network)."""
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event

from fake_caldav import FakeOC
from x16hub import calsync as C
from x16hub.backend.owncloud import OCError
from x16hub.store import HubError

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 17)
PET = C.Petscii()
REPO_DAT = Path(__file__).resolve().parents[2] / "apps" / "calendar" / "CALENDAR.DAT"


class X16:
    """Simulates the X16 app + its future Sync button (whole-file push, then apply the pull)."""

    def __init__(self):
        self.recs = [C.EMPTY] * C.NSLOTS
        self.counter = 0

    def add(self, d: date, text: str) -> int:
        s = next(i for i, r in enumerate(self.recs) if r[0] == 0)      # app: lowest free slot
        self.recs[s] = C.X16Rec.make(d, PET.encode(text), 1).to_bytes()
        return s

    def edit(self, slot: int, d: date | None = None, text: str | None = None):
        r = C.X16Rec.from_bytes(self.recs[slot])
        self.recs[slot] = C.X16Rec.make(d or r.date, PET.encode(text) if text is not None else r.text,
                                        r.flags | C.F_EDITED).to_bytes()

    def delete(self, slot: int):
        self.recs[slot] = C.EMPTY

    def find(self, text: str):
        return [i for i, r in enumerate(self.recs) if r[0] and PET.decode(C.X16Rec.from_bytes(r).text) == text]

    def text(self, slot):
        return PET.decode(C.X16Rec.from_bytes(self.recs[slot]).text)

    def rec(self, slot):
        return C.X16Rec.from_bytes(self.recs[slot])

    def used(self):
        return [i for i, r in enumerate(self.recs) if r[0]]

    def sync(self, cs, apply=True):
        push, pull = cs.sync(list(enumerate(self.recs)), self.counter)
        if apply:
            for s, raw in pull.records:
                self.recs[s] = raw
            self.counter = pull.counter
        return push, pull


@pytest.fixture
def env(tmp_path):
    oc = FakeOC()
    db = sqlite3.connect(tmp_path / "hub.db")
    cs = C.CalSync(oc, db, today=lambda: TODAY, lock_path=tmp_path / "lock")
    return oc, cs, X16()


def vevent(uid, start, summary, end=None, **extra) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//test//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    ev.add("dtstart", start)
    if end is not None:
        ev.add("dtend", end)
    ev.add("dtstamp", datetime(2026, 1, 1, tzinfo=timezone.utc))
    for k, v in extra.items():
        ev.add(k.replace("_", "-"), v)
    cal.add_component(ev)
    return cal.to_ical()


def server_ev(oc, uid):
    cal = Calendar.from_ical(oc.ics(uid))
    return [e for e in cal.walk("VEVENT")]


def edit_server(oc, uid, fn):
    cal = Calendar.from_ical(oc.ics(uid))
    fn(cal)
    oc.server_put(uid, cal.to_ical())


def by_summary(oc, summary):
    out = []
    for h, (ics, _) in oc.objs.items():
        for e in Calendar.from_ical(ics).walk("VEVENT"):
            if str(e.get("summary")) == summary:
                out.append(e)
    return out


# --------------------------------------------------------------------------- charset / format

def test_petscii_mapping():
    assert PET.encode("Go Somewhere") == bytes([0xC7, 0x4F, 0x20, 0xD3, 0x4F, 0x4D, 0x45, 0x57, 0x48, 0x45, 0x52, 0x45])
    assert PET.decode(b"GO SOMEWHERE") == "go somewhere"           # unshifted keys -> lowercase glyphs
    assert PET.decode(PET.encode("Meet Bob @ 10:30 [2]!")) == "Meet Bob @ 10:30 [2]!"
    assert PET.encode("£↑←") == b"\x5c\x5e\x5f"
    assert PET.decode(PET.encode("Café ☕ Zoë Øl naïve €5")) == "Cafe ? Zoe Ol naive E5"
    for s in ["Café ☕ Zoë", "x" * 40, "日本語"]:
        assert len(PET.encode(s)) == len(s)                          # strictly 1 char : 1 byte


def test_iso_charset():
    iso = C.Iso885915()
    assert iso.encode("Café €5 ☕") == b"Caf\xe9 \xa45 ?"
    assert iso.decode(b"Caf\xe9 \xa45") == "Café €5"


def test_repo_sample_dat():
    hdr, recs = C.read_dat(REPO_DAT.read_bytes())
    assert hdr == b"\x55\x1a" and len(recs) == 128
    r = C.X16Rec.from_bytes(recs[0])
    assert (r.used, r.date, PET.decode(r.text)) == (True, date(2000, 1, 6), "go somewhere")
    assert not any(C.X16Rec.from_bytes(x).used for x in recs[1:])
    assert C.write_dat(hdr, recs) == REPO_DAT.read_bytes()


def test_record_roundtrip_and_validation():
    r = C.X16Rec.make(date(2026, 12, 25), b"x" * 40)
    b = r.to_bytes()
    assert len(b) == 32 and b[4 + 27] == 0 and C.X16Rec.from_bytes(b).text == b"x" * 27
    with pytest.raises(HubError):
        C.X16Rec.from_bytes(b"\x01" * 31)
    with pytest.raises(HubError):
        C.CalSync._validate([(128, C.EMPTY)])
    with pytest.raises(HubError):
        C.CalSync._validate([(1, C.EMPTY), (1, C.EMPTY)])


def test_split_and_merge_visible():
    assert C.split_text(b"09:05 x") == ((9, 5), b"x")
    assert C.split_text(b"9:05 x") == ((9, 5), b"x")
    assert C.split_text(b"25:00 x") == (None, b"25:00 x")
    full = "Café meeting with Zoë about the Q4 roadmap and budget"
    vis, n = C.render_text(PET, None, full)
    assert n == 27 and PET.decode(vis) == "Cafe meeting with Zoe about"
    new = PET.encode("Cafe lunch with Zoe about")
    assert C.merge_visible(PET, full, vis, new) == "Café lunch with Zoë about the Q4 roadmap and budget"


# --------------------------------------------------------------------------- X16 -> server

def test_x16_create_allday_and_timed(env):
    oc, cs, x = env
    x.add(date(2026, 11, 20), "Dentist")
    x.add(date(2026, 11, 21), "14:30 Bike ride")
    push, pull = x.sync(cs)
    assert push.created == 2 and len(oc.objs) == 2
    (a,) = by_summary(oc, "Dentist")
    assert a.decoded("dtstart") == date(2026, 11, 20) and a.decoded("dtend") == date(2026, 11, 21)
    (b,) = by_summary(oc, "Bike ride")
    st = b.decoded("dtstart")
    assert st == datetime(2026, 11, 21, 14, 30, tzinfo=NY) and str(st.tzinfo) == "America/New_York"
    assert b.decoded("dtend") - st == timedelta(hours=1)
    assert "BEGIN:VTIMEZONE" in oc.ics(str(b["uid"]))
    # nothing more happens on a second sync
    oc.calls.clear()
    push, pull = x.sync(cs)
    assert push.created == 0 and "create" not in oc.calls and pull.records == []


def test_server_create_maps_to_x16_with_ny_time(env):
    oc, cs, x = env
    x.sync(cs)
    oc.server_put("a", vevent("a", datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc), "BBQ"))   # EDT -> 11:00
    oc.server_put("b", vevent("b", datetime(2026, 12, 1, 3, 30, tzinfo=timezone.utc), "Late"))  # EST -> Nov 30 22:30
    oc.server_put("c", vevent("c", date(2026, 11, 26), "Thanksgiving", end=date(2026, 11, 27)))
    x.sync(cs)
    (s,) = x.find("11:00 BBQ")
    assert x.rec(s).date == date(2026, 7, 4)
    (s,) = x.find("22:30 Late")
    assert x.rec(s).date == date(2026, 11, 30)
    (s,) = x.find("Thanksgiving")
    assert x.rec(s).date == date(2026, 11, 26)


def test_server_edit_reaches_x16_and_x16_untouched_fields_survive(env):
    oc, cs, x = env
    oc.server_put("e1", vevent("e1", datetime(2026, 11, 18, 9, 0, tzinfo=NY), "Standup",
                               end=datetime(2026, 11, 18, 9, 15, tzinfo=NY),
                               location="Room 4", description="Daily sync"))
    x.sync(cs)
    (s,) = x.find("09:00 Standup")
    edit_server(oc, "e1", lambda cal: (cal.walk("VEVENT")[0].pop("SUMMARY"),
                                       cal.walk("VEVENT")[0].add("summary", "Standup (moved)")))
    x.sync(cs)
    assert x.text(s) == "09:00 Standup (moved)"
    ev = server_ev(oc, "e1")[0]
    assert str(ev["location"]) == "Room 4" and str(ev["description"]) == "Daily sync"


def test_deletes_both_ways(env):
    oc, cs, x = env
    s1 = x.add(date(2026, 11, 20), "From X16")
    oc.server_put("srv", vevent("srv", date(2026, 11, 22), "From server", end=date(2026, 11, 23)))
    x.sync(cs)
    (s2,) = x.find("From server")
    # X16 deletes the server-born one; ownCloud deletes the X16-born one
    x.delete(s2)
    uid1 = str(by_summary(oc, "From X16")[0]["uid"])
    oc.server_delete(uid1)
    push, pull = x.sync(cs)
    assert push.deleted == 1 and oc.objs == {}
    assert x.used() == [] and push.conflicts == 0


def test_replaced_slot_is_delete_plus_create(env):
    """The app reuses the lowest free slot: delete + add on the same day must not be an 'edit'."""
    oc, cs, x = env
    oc.server_put("d", vevent("d", datetime(2026, 11, 19, 8, tzinfo=NY), "Dentist", location="Main St"))
    x.sync(cs)
    (s,) = x.find("08:00 Dentist")
    x.delete(s)
    assert x.add(date(2026, 11, 19), "Lunch") == s
    push, _ = x.sync(cs)
    assert push.deleted == 1 and push.created == 1
    (lunch,) = by_summary(oc, "Lunch")
    assert "location" not in lunch and "d" not in [str(e["uid"]) for e in by_summary(oc, "Dentist")]


# --------------------------------------------------------------------------- conflicts

def test_conflict_x16_delete_vs_server_edit_keeps_edit(env):
    oc, cs, x = env
    oc.server_put("k", vevent("k", date(2026, 11, 25), "Party"))
    x.sync(cs)
    (s,) = x.find("Party")
    x.delete(s)                                                   # offline on the X16 ...
    edit_server(oc, "k", lambda cal: cal.walk("VEVENT")[0].add("location", "Bob's"))   # ... and in ownCloud
    push, pull = x.sync(cs)
    assert push.deleted == 0 and push.conflicts == 1
    assert oc.href_for("k") in oc.objs
    (s2,) = x.find("Party")
    assert x.rec(s2).flags & C.F_CONFLICT
    (c,) = pull.conflicts
    assert c["kind"] == "x16-delete-vs-server-edit" and c["kept"] == "server"
    assert cs.ack_conflicts([s2]) == 1
    x.sync(cs)
    assert not x.rec(s2).flags & C.F_CONFLICT


def test_field_level_merge_no_conflict(env):
    oc, cs, x = env
    oc.server_put("m", vevent("m", datetime(2026, 11, 20, 10, tzinfo=NY), "Review",
                              end=datetime(2026, 11, 20, 11, 30, tzinfo=NY), description="agenda"))
    x.sync(cs)
    (s,) = x.find("10:00 Review")
    x.edit(s, d=date(2026, 11, 21))                                # X16 moves the day
    edit_server(oc, "m", lambda cal: (cal.walk("VEVENT")[0].pop("SUMMARY"),
                                      cal.walk("VEVENT")[0].add("summary", "Design review")))
    push, pull = x.sync(cs)
    assert push.updated == 1 and push.conflicts == 0
    ev = server_ev(oc, "m")[0]
    assert str(ev["summary"]) == "Design review"
    assert ev.decoded("dtstart") == datetime(2026, 11, 21, 10, tzinfo=NY)
    assert ev.decoded("dtend") - ev.decoded("dtstart") == timedelta(minutes=90)    # duration kept
    assert str(ev["description"]) == "agenda" and int(ev["sequence"]) >= 1
    assert x.text(s) == "10:00 Design review" and x.rec(s).date == date(2026, 11, 21)
    assert not x.rec(s).flags & C.F_EDITED


def test_field_level_conflict_lww_keeps_loser(env):
    oc, cs, x = env
    oc.server_put("t", vevent("t", date(2026, 11, 28), "Hike"))
    x.sync(cs)
    (s,) = x.find("Hike")
    x.edit(s, text="Hike Patapsco")
    edit_server(oc, "t", lambda cal: (cal.walk("VEVENT")[0].pop("SUMMARY"),
                                      cal.walk("VEVENT")[0].add("summary", "Hike Sugarloaf")))
    push, pull = x.sync(cs)
    assert push.conflicts == 1
    ev = server_ev(oc, "t")[0]
    assert str(ev["summary"]) == "Hike Patapsco"
    assert "Hike Sugarloaf" in str(ev["description"])               # loser preserved in ownCloud
    (c,) = pull.conflicts
    assert (c["field"], c["x16_value"], c["server_value"], c["kept"]) == ("text", "Hike Patapsco", "Hike Sugarloaf", "x16")
    assert x.rec(s).flags & C.F_CONFLICT


def test_x16_edit_vs_server_delete_recreates(env):
    oc, cs, x = env
    oc.server_put("g", vevent("g", date(2026, 11, 29), "Game"))
    x.sync(cs)
    (s,) = x.find("Game")
    x.edit(s, text="Game night")
    oc.server_delete("g")
    push, pull = x.sync(cs)
    assert push.created == 1 and push.conflicts == 1
    assert len(by_summary(oc, "Game night")) == 1


def test_edit_preserves_hidden_fields_and_unicode(env):
    oc, cs, x = env
    full = "Café with Zoë ☕ to plan the 2027 garden layout"
    ics = vevent("u", datetime(2026, 11, 23, 12, tzinfo=NY), full, location="Bäckerei", description="Bring €20",
                 x_custom="keep-me")
    cal = Calendar.from_ical(ics)
    from icalendar import Alarm
    al = Alarm()
    al.add("action", "DISPLAY")
    al.add("trigger", timedelta(minutes=-15))
    cal.walk("VEVENT")[0].add_component(al)
    oc.server_put("u", cal.to_ical())
    x.sync(cs)
    (s,) = [i for i in x.used()]
    assert x.text(s) == "12:00 Cafe with Zoe ? to pl"           # 27 chars
    x.edit(s, text="12:00 Cafe with Zoe ? TO pl")
    x.sync(cs)
    ev = server_ev(oc, "u")[0]
    assert str(ev["summary"]) == "Café with Zoë ☕ TO plan the 2027 garden layout"
    assert str(ev["location"]) == "Bäckerei" and str(ev["description"]) == "Bring €20"
    assert str(ev["x-custom"]) == "keep-me" and len(ev.walk("VALARM")) == 1


def test_untouched_roundtrip_never_writes(env):
    oc, cs, x = env
    oc.server_put("long", vevent("long", date(2026, 11, 30), "A very long summary that the X16 cannot show fully"))
    for _ in range(3):
        x.sync(cs)
    assert "update" not in oc.calls and "delete" not in oc.calls


# --------------------------------------------------------------------------- idempotency / interruption

def test_lost_pull_reply_no_dupes_no_deletes(env):
    oc, cs, x = env
    x.add(date(2026, 11, 20), "One")
    x.sync(cs, apply=False)                    # hub did the work, X16 never got the reply
    x.sync(cs, apply=False)
    x.sync(cs)
    assert len(oc.objs) == 1 and x.find("One")
    # hub-side placements also unacked, then X16 deletes the record it did have
    oc.server_put("s", vevent("s", date(2026, 11, 21), "Srv"))
    x.sync(cs, apply=False)                    # hub placed "Srv" into a slot; X16 didn't hear
    (s1,) = x.find("One")
    x.delete(s1)
    x.sync(cs)
    assert [str(e["summary"]) for h in oc.objs for e in Calendar.from_ical(oc.objs[h][0]).walk("VEVENT")] == ["Srv"]
    assert x.find("Srv") and not x.find("One")


def test_crash_after_server_create_does_not_duplicate(env):
    oc, cs, x = env
    x.add(date(2026, 11, 20), "Once")
    oc.fail_after = "create"
    with pytest.raises(OCError):
        x.sync(cs)                             # server got it, hub.db rolled back
    assert len(oc.objs) == 1
    x.sync(cs)
    assert len(oc.objs) == 1 and len(x.find("Once")) == 1


def test_crash_after_server_delete_is_idempotent(env):
    oc, cs, x = env
    oc.server_put("z", vevent("z", date(2026, 11, 20), "Zap"))
    x.sync(cs)
    (s,) = x.find("Zap")
    x.delete(s)
    oc.fail_after = "delete"
    with pytest.raises(OCError):
        x.sync(cs)
    push, _ = x.sync(cs)
    assert oc.objs == {} and x.used() == [] and push.conflicts == 0


def test_lost_hub_db_adopts_instead_of_duplicating(env, tmp_path):
    oc, cs, x = env
    x.add(date(2026, 11, 20), "Keep")
    x.add(date(2026, 11, 20), "10:00 Keep2")
    x.sync(cs)
    cs2 = C.CalSync(oc, sqlite3.connect(tmp_path / "new.db"), today=lambda: TODAY, lock_path=tmp_path / "l2")
    push, pull = x.sync(cs2)                   # counter unknown to new db -> full resync
    assert push.created == 0 and len(oc.objs) == 2 and pull.full
    assert len(x.find("Keep")) == 1 and len(x.find("10:00 Keep2")) == 1


def test_invalid_sync_token_falls_back_to_full(env):
    oc, cs, x = env
    x.sync(cs)
    oc.server_put("q", vevent("q", date(2026, 11, 20), "Q"))
    oc.reject_tokens = True
    x.sync(cs)
    assert x.find("Q")


def test_unsyncable_record_stays_local(env):
    oc, cs, x = env
    x.recs[5] = bytes([1, 26, 2, 30]) + b"BAD DATE".ljust(28, b"\0")   # Feb 30
    push, pull = x.sync(cs)
    assert push.unsyncable == 1 and oc.objs == {}
    assert x.recs[5][:4] == bytes([1, 26, 2, 30])
    oc.server_put("n", vevent("n", date(2026, 11, 20), "N"))
    x.sync(cs)
    assert x.find("N") != [5]                                       # slot 5 is never overwritten


# --------------------------------------------------------------------------- recurring / capacity

def weekly(uid, start, summary, **kw):
    return vevent(uid, start, summary, end=start + timedelta(hours=1), rrule={"freq": "weekly", "count": 10}, **kw)


def test_recurring_occurrences_exdate_and_override(env):
    oc, cs, x = env
    oc.server_put("w", weekly("w", datetime(2026, 11, 2, 19, 0, tzinfo=NY), "Club"))   # Mondays, crosses no DST
    x.sync(cs)
    occ = sorted((x.rec(s).date, s) for s in x.find("19:00 Club"))
    assert [d for d, _ in occ] == [date(2026, 11, 2) + timedelta(weeks=i) for i in range(10)]
    # delete one occurrence on the X16 -> EXDATE
    x.delete(occ[3][1])
    # edit another occurrence on the X16 -> override with RECURRENCE-ID
    x.edit(occ[4][1], text="19:00 Club (picnic)")
    push, _ = x.sync(cs)
    assert push.deleted == 1 and push.updated == 1
    evs = server_ev(oc, "w")
    master = next(e for e in evs if "recurrence-id" not in e)
    ov = next(e for e in evs if "recurrence-id" in e)
    assert [d.dt for d in master["exdate"].dts] == [datetime(2026, 11, 23, 19, tzinfo=NY)]
    assert ov.decoded("recurrence-id") == datetime(2026, 11, 30, 19, tzinfo=NY)
    assert str(ov["summary"]) == "Club (picnic)" and "rrule" not in ov
    assert len(x.find("19:00 Club")) == 8 and len(x.find("19:00 Club (picnic)")) == 1
    oc.calls.clear()
    x.sync(cs)
    assert "update" not in oc.calls


def test_recurring_dst_wall_clock_and_floating(env):
    oc, cs, x = env
    oc.server_put("d", weekly("d", datetime(2026, 10, 26, 18, 0, tzinfo=NY), "Run"))   # crosses Nov 1 DST end
    oc.server_put("f", vevent("f", datetime(2026, 11, 3, 7, 0), "Float", rrule={"freq": "daily", "count": 2}))
    x.sync(cs)
    assert len(x.find("18:00 Run")) == 10 and len(x.find("07:00 Float")) == 2


def test_capacity_maxday_and_128(env):
    oc, cs, x = env
    for i in range(8):
        oc.server_put(f"day{i}", vevent(f"day{i}", datetime(2026, 11, 20, 8 + i, tzinfo=NY), f"Slot {i}"))
    _, pull = x.sync(cs)
    assert len([s for s in x.used() if x.rec(s).date == date(2026, 11, 20)]) == 6 and pull.unplaced == 2
    for i in range(130):
        d = TODAY + timedelta(days=1 + i)
        oc.server_put(f"n{i}", vevent(f"n{i}", d, f"Ev {i}"))
    _, pull = x.sync(cs)
    assert len(x.used()) == 128 and pull.unplaced == 138 - 128
    assert x.find("Ev 0") and not x.find("Ev 129")                 # nearest upcoming first


def test_old_events_evicted_from_x16_not_server(env):
    oc, cs, x = env
    for i in range(128):
        oc.server_put(f"old{i}", vevent(f"old{i}", date(2025, 1, 1) + timedelta(days=i), f"Old {i}"))
    x.sync(cs)
    assert len(x.used()) == 128
    oc.server_put("new", vevent("new", TODAY + timedelta(days=3), "New thing"))
    x.sync(cs)
    assert x.find("New thing") and not x.find("Old 0") and len(oc.objs) == 129


# --------------------------------------------------------------------------- file mode

def test_sync_dat_file(env, tmp_path):
    oc, cs, _ = env
    dat = tmp_path / "CALENDAR.DAT"
    dat.write_bytes(REPO_DAT.read_bytes())
    push, pull = C.sync_dat_file(cs, dat)
    assert push.created == 1 and len(by_summary(oc, "go somewhere")) == 1
    assert dat.stat().st_size == 4098 and (tmp_path / "CALSYNC.DAT").read_bytes() == pull.counter.to_bytes(4, "little")
    oc.server_put("p", vevent("p", date(2026, 12, 24), "Café ☕"))
    C.sync_dat_file(cs, dat)
    _, recs = C.read_dat(dat.read_bytes())
    assert any(C.X16Rec.from_bytes(r).text == PET.encode("Cafe ?") for r in recs)


def test_x16_create_races_unacked_hub_placement(env):
    oc, cs, x = env
    x.sync(cs)
    oc.server_put("r", vevent("r", date(2026, 11, 20), "Server one"))
    x.sync(cs, apply=False)                    # hub put "Server one" in slot 0; X16 never got it
    assert x.add(date(2026, 11, 21), "X16 one") == 0   # X16 uses slot 0 too
    x.sync(cs)
    assert len(oc.objs) == 2 and x.find("X16 one") == [0] and len(x.find("Server one")) == 1


def test_x16_adds_time_to_allday_long_summary(env):
    oc, cs, x = env
    full = "Annual neighbourhood potluck dinner at the park"
    oc.server_put("l", vevent("l", date(2026, 11, 27), full, end=date(2026, 11, 28)))
    x.sync(cs)
    (s,) = x.used()
    x.edit(s, text="18:00 " + x.text(s))       # app's line editor would truncate to 27
    push, _ = x.sync(cs)
    ev = server_ev(oc, "l")[0]
    assert str(ev["summary"]) == full                                 # truncation is not a text edit
    assert ev.decoded("dtstart") == datetime(2026, 11, 27, 18, tzinfo=NY)
    assert ev.decoded("dtend") == datetime(2026, 11, 27, 19, tzinfo=NY)
    assert push.conflicts == 0


def test_conflicts_api_and_status(env):
    oc, cs, x = env
    oc.server_put("k", vevent("k", date(2026, 11, 25), "Party"))
    x.sync(cs)
    (s,) = x.find("Party")
    x.delete(s)
    edit_server(oc, "k", lambda cal: cal.walk("VEVENT")[0].add("location", "Bob's"))
    x.sync(cs)
    st = cs.status()
    assert st["open_conflicts"] == 1 and st["x16_used"] == 1 and st["server_objects"] == 1
    assert cs.ack_conflicts() == 1 and cs.conflicts() == [] and len(cs.conflicts(include_acked=True)) == 1


def test_pathological_recurrence_is_bounded(env):
    import time
    oc, cs, x = env
    oc.server_put("sec", vevent("sec", datetime(2000, 1, 1, tzinfo=NY), "Every second", rrule={"freq": "secondly"}))
    oc.server_put("hr", vevent("hr", datetime(2026, 11, 1, tzinfo=NY), "Hourly", rrule={"freq": "hourly"}))
    t = time.time()
    x.sync(cs)
    assert time.time() - t < 10
    assert not x.find("Every second")
    days = [x.rec(s).date for s in x.used()]
    assert 96 <= len(days) <= 102 and max(days.count(d) for d in days) == 6   # 400-occurrence cap, 6/day
