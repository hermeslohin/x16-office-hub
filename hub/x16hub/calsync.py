"""Two-way sync between the X16 calendar (CALENDAR.DAT records) and an ownCloud CalDAV calendar.

The record format is in apps/calendar/FORMAT.md and the X16/UI side in apps/calendar/SYNC-PLAN.md.
In short: 128 slots of 32 bytes each. +0 is the flag byte (0 = free, bit0 used, bit1 edited in
place, bit2 conflict), +1 is year-2000, +2 the month, +3 the day, and +4..+31 hold the text
(at most 27 chars, NUL-terminated). The text is PETSCII in the current app (lowercase charset);
ISO-8859-15 is optional. A text that starts with "HH:MM " means a timed event in
America/New_York; any other text means an all-day event.

Design
------
* The slot number (0..127) is the X16 record ID. The hub keeps a *version history per slot*
  (cal_slot_ver). Every hub-side change to a slot gets the next value of a global counter
  (`rev`). The X16 stores that counter (CALSYNC.DAT) and sends it back as `since`.
  So the hub always knows which version of a slot the X16 has (its *base*): the newest
  version with rev <= since, or one the X16 itself sent (origin 'x16'). That makes
  every op idempotent and safe to interrupt. A push that is sent again, or a pull reply
  that never arrived, is recognised and never turns into a duplicate or a lost delete.
* Server side: an RFC 6578 sync-collection mirror of every calendar object (cal_objects,
  holding the full ICS). Edits are always applied to the stored ICS, so every property
  the X16 cannot hold (description, location, alarms, attendees, the hidden end of a
  long summary, accents, X- props) is kept.
* X16 changes. A free slot becoming used is a CREATE (a deterministic UID, so a retry
  can't make a duplicate). A used slot becoming free is a DELETE. A used slot with
  different content is a REPLACE (delete + create), because the current app can only
  add/delete and the old app reuses the lowest free slot. If the record has the
  "edited" bit (a future in-place edit in the app), it is a field-level EDIT instead.
* Conflict rule (never silently lose an edit):
    - X16 delete vs. server edit: the server version is kept and comes back to the X16
      with the conflict bit set.
    - X16 edit vs. server delete: the event is recreated from the X16 record and flagged.
    - Edits on both sides: merged field by field (date, time, text). If the same field
      changed on both sides, the last writer wins, and that is the X16 because its
      change reached the hub last. The losing value goes to cal_conflicts and is
      appended to the event DESCRIPTION, and the conflict bit is set.
* Server events become X16 records: singles always, recurring events as occurrences in
  [today-31d, today+366d], within years 2000..2200, at most 6 per day and 128 in total,
  nearest-upcoming first. Past events older than 31 days are evicted from the X16 (not
  from the server) when room is needed. Deleting an occurrence on the X16 adds an EXDATE.

Wire-layer interface (W5): `CalSync.apply_x16_changes(records, since)`,
`CalSync.x16_changes_since(counter)`, `CalSync.sync(records, since)`,
`CalSync.conflicts()`, `CalSync.ack_conflicts(slots)`, `CalSync.status()`. The wire layer
just moves (slot, 32-byte record) pairs plus a u32 counter.
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from .store import HubError, connect as store_connect, data_dir

log = logging.getLogger("x16hub.calsync")

NY = ZoneInfo("America/New_York")
NSLOTS, RECSZ, TXTMAX, MAXDAY = 128, 32, 27, 6
FILESZ = 2 + NSLOTS * RECSZ           # PRG header + 4096
DEFAULT_HEADER = b"\x55\x1a"          # load address the app's slab had when built ($1A55); ignored on load
YEAR_MIN, YEAR_MAX = 2000, 2200       # what the app can navigate to (year byte 0..200)
F_USED, F_EDITED, F_CONFLICT = 0x01, 0x02, 0x04
WINDOW_PAST, WINDOW_FUTURE = 31, 366  # days; recurring expansion + eviction horizon
MAX_EXPAND = 400                      # occurrences per recurring event, hard cap
DEFAULT_CAL = "x16"
_TIME_RE = re.compile(rb"^([0-9]{1,2}):([0-9]{2}) ")


# =========================================================================== charsets

_TRANSLIT = {"ß": "s", "æ": "a", "Æ": "A", "ø": "o", "Ø": "O", "œ": "o", "Œ": "O", "ð": "d", "Ð": "D",
             "þ": "t", "Þ": "T", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "€": "E",
             "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"', "–": "-", "—": "-",
             "…": ".", "\u00a0": " ", "•": "*", "·": ".", "×": "x", "÷": "/"}


def _ascii_fold(ch: str) -> str | None:
    """One printable ASCII char for ch, or None."""
    if " " <= ch <= "~":
        return ch
    if ch in _TRANSLIT:
        return _TRANSLIT[ch]
    base = unicodedata.normalize("NFKD", ch)[:1]
    if base and " " <= base <= "~":
        return base
    return None


class Charset:
    """1 char <-> 1 byte, always. That keeps visible text aligned with the server summary for merges."""
    name = "abstract"

    def enc_char(self, ch: str) -> int:
        raise NotImplementedError

    def dec_byte(self, b: int) -> str:
        raise NotImplementedError

    def encode(self, s: str) -> bytes:
        return bytes(self.enc_char(c) for c in s)

    def decode(self, b: bytes) -> str:
        return "".join(self.dec_byte(x) for x in b)


class Petscii(Charset):
    """PETSCII as the X16 calendar uses it: lowercase/uppercase charset, keyboard codes 32..126 and 193..218.

    $41-$5A show as a-z, $C1-$DA as A-Z, $5C=£ $5E=↑ $5F=←. $61-$7A are the C64 duplicates of
    $C1-$DA (A-Z). Other graphics codes decode to '?'.
    """
    name = "petscii"
    _SUBST = {"\\": "/", "{": "(", "}": ")", "|": "!", "~": "-", "`": "'", "_": "-", "^": "↑"}

    def enc_char(self, ch: str) -> int:
        if ch == "£":
            return 0x5C
        if ch == "↑":
            return 0x5E
        if ch == "←":
            return 0x5F
        a = _ascii_fold(ch)
        if a is None:
            return 0x3F
        a = self._SUBST.get(a, a)
        if a == "↑":
            return 0x5E
        o = ord(a)
        if 0x61 <= o <= 0x7A:      # a-z
            return o - 0x20
        if 0x41 <= o <= 0x5A:      # A-Z
            return o + 0x80
        return o                   # 0x20-0x40, [ ]

    def dec_byte(self, b: int) -> str:
        if 0x41 <= b <= 0x5A:
            return chr(b + 0x20)
        if 0xC1 <= b <= 0xDA:
            return chr(b - 0x80)
        if 0x61 <= b <= 0x7A:
            return chr(b - 0x20)
        if b == 0x5C:
            return "£"
        if b == 0x5E:
            return "↑"
        if b == 0x5F:
            return "←"
        if 0x20 <= b <= 0x40 or b in (0x5B, 0x5D):
            return chr(b)
        return "?"


class Iso885915(Charset):
    """For an app that switches to ISO mode (txt.iso()). Not what the current app does."""
    name = "iso-8859-15"

    def enc_char(self, ch: str) -> int:
        try:
            b = ch.encode("iso8859_15")
            if len(b) == 1 and (0x20 <= b[0] <= 0x7E or 0xA0 <= b[0]):
                return b[0]
        except UnicodeEncodeError:
            pass
        a = _ascii_fold(ch)
        return ord(a) if a else 0x3F

    def dec_byte(self, b: int) -> str:
        if 0x20 <= b <= 0x7E or b >= 0xA0:
            return bytes([b]).decode("iso8859_15")
        return "?"


CHARSETS = {"petscii": Petscii(), "iso-8859-15": Iso885915()}


# =========================================================================== X16 records

@dataclass(frozen=True)
class X16Rec:
    flags: int = 0
    year: int = 0          # full year
    month: int = 0
    day: int = 0
    text: bytes = b""      # without NUL, <= 27 bytes

    @property
    def used(self) -> bool:
        return self.flags != 0          # the app tests "== 0" for a free slot

    @property
    def date(self) -> date | None:
        try:
            return date(self.year, self.month, self.day)
        except ValueError:
            return None

    def norm(self) -> tuple:
        """What counts as content (status bits ignored)."""
        return (True, self.year, self.month, self.day, self.text) if self.used else (False,)

    @classmethod
    def from_bytes(cls, b: bytes) -> "X16Rec":
        if len(b) != RECSZ:
            raise HubError(f"X16 record must be {RECSZ} bytes, got {len(b)}")
        if b[0] == 0:
            return cls()
        txt = b[4:4 + TXTMAX + 1].split(b"\0", 1)[0][:TXTMAX]
        return cls(b[0], 2000 + b[1], b[2], b[3], txt)

    @classmethod
    def make(cls, d: date, text: bytes, flags: int = F_USED) -> "X16Rec":
        return cls(flags | F_USED, d.year, d.month, d.day, text[:TXTMAX])

    def to_bytes(self) -> bytes:
        if not self.used:
            return bytes(RECSZ)
        out = bytes([self.flags, (self.year - 2000) & 0xFF, self.month, self.day]) + self.text[:TXTMAX]
        return out.ljust(RECSZ, b"\0")


EMPTY = bytes(RECSZ)


def split_text(text: bytes) -> tuple[tuple[int, int] | None, bytes]:
    """'HH:MM rest' -> ((h, m), rest); otherwise (None, text)."""
    m = _TIME_RE.match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 24 and mi < 60:
            return (h, mi), text[m.end():]
    return None, text


def clean_summary(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "")
    return "".join(" " if (c in "\r\n\t" or unicodedata.category(c) == "Cc") else c for c in s)


def render_text(cs: Charset, tm: tuple[int, int] | None, summary: str) -> tuple[bytes, int]:
    """X16 text bytes for an event and how many summary chars are visible."""
    prefix = b"%02d:%02d " % tm if tm else b""
    room = TXTMAX - len(prefix)
    s = clean_summary(summary)
    vis = cs.encode(s[:room])
    return prefix + vis, len(vis)


def merge_visible(cs: Charset, full: str, old_vis: bytes, new_vis: bytes) -> str:
    """Apply the X16 edit old_vis -> new_vis to the full server summary.

    Unchanged runs keep the original Unicode chars (accents, emoji). The part of the summary
    the X16 never saw (past 27 chars) is kept as is.
    """
    full = clean_summary(full)
    n = len(old_vis)
    orig, tail = full[:n], full[n:]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_vis, new_vis, autojunk=False).get_opcodes():
        out.append(orig[i1:i2] if tag == "equal" else cs.decode(new_vis[j1:j2]))
    return "".join(out) + tail


def read_dat(blob: bytes) -> tuple[bytes, list[bytes]]:
    """CALENDAR.DAT -> (2-byte header, 128 raw records). Short files are zero-padded like the app does."""
    if len(blob) < 2:
        return DEFAULT_HEADER, [EMPTY] * NSLOTS
    if len(blob) > FILESZ:
        raise HubError(f"CALENDAR.DAT is {len(blob)} bytes; max {FILESZ}")
    hdr, body = blob[:2], blob[2:].ljust(NSLOTS * RECSZ, b"\0")
    return hdr, [body[i * RECSZ:(i + 1) * RECSZ] for i in range(NSLOTS)]


def write_dat(hdr: bytes, recs: list[bytes]) -> bytes:
    assert len(recs) == NSLOTS and all(len(r) == RECSZ for r in recs)
    return (hdr or DEFAULT_HEADER) + b"".join(recs)


# =========================================================================== server items

@dataclass
class Item:
    uid: str
    rid: str                 # '' = single event; else original start (YYYYMMDD or YYYYMMDDTHHMMSSZ)
    href: str
    etag: str | None
    date: date
    tm: tuple[int, int] | None
    summary: str
    recurring: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.uid, self.rid)

    def shash(self) -> str:
        return hashlib.sha256(f"{self.date}|{self.tm}|{clean_summary(self.summary)}".encode()).hexdigest()[:32]


def _to_ny(v) -> tuple[date, tuple[int, int] | None]:
    if isinstance(v, datetime):
        v = v.replace(tzinfo=NY) if v.tzinfo is None else v.astimezone(NY)   # floating = NY
        return v.date(), (v.hour, v.minute)
    return v, None


def _rid_str(v) -> str:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=NY)
        return v.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return v.strftime("%Y%m%d")


def _rid_value(rid: str, like):
    """Rebuild a RECURRENCE-ID/EXDATE value of the same kind as DTSTART `like`."""
    if len(rid) == 8:
        return datetime.strptime(rid, "%Y%m%d").date()
    v = datetime.strptime(rid, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if isinstance(like, datetime) and like.tzinfo is not None:
        return v.astimezone(like.tzinfo)
    if isinstance(like, datetime):
        return v.astimezone(NY).replace(tzinfo=None)
    return v.date()


def _cancelled(ev) -> bool:
    return str(ev.get("status", "")).upper() == "CANCELLED"


def _expand(master, win_start: date, win_end: date) -> list:
    """Occurrence start values (date or aware datetime) of a recurring VEVENT within [win_start, win_end)."""
    from dateutil.rrule import rruleset, rrulestr
    start = master.decoded("dtstart")
    allday = not isinstance(start, datetime)
    naive = allday or start.tzinfo is None          # all-day and floating series expand in wall time
    s0 = datetime.combine(start, dtime()) if allday else start

    def conv(v):
        if allday:
            return datetime.combine(v if not isinstance(v, datetime) else v.date(), dtime())
        if not isinstance(v, datetime):
            v = datetime.combine(v, s0.time())
        if naive:
            return v.astimezone(NY).replace(tzinfo=None) if v.tzinfo else v
        return v if v.tzinfo else v.replace(tzinfo=s0.tzinfo)

    rs = rruleset()
    rules = master.get("rrule")
    rules = rules if isinstance(rules, list) else ([rules] if rules is not None else [])
    for r in rules:
        txt = r.to_ical().decode()
        if re.search(r"FREQ=(SECONDLY|MINUTELY)", txt, re.I):
            raise ValueError("sub-hourly recurrence is not mapped to the X16")   # also bounds CPU time
        if allday:   # UNTIL with a 'Z' on an all-day series: dateutil wants naive
            txt = re.sub(r"UNTIL=(\d{8})T\d{6}Z?", r"UNTIL=\1", txt)
        elif naive:  # floating series: UNTIL must be naive too
            txt = re.sub(r"(UNTIL=\d{8}T\d{6})Z", r"\1", txt)
        elif re.search(r"UNTIL=\d{8}(;|$)", txt):   # aware series with a DATE until -> end of that day UTC
            txt = re.sub(r"UNTIL=(\d{8})(;|$)", r"UNTIL=\1T235959Z\2", txt)
        rs.rrule(rrulestr(txt, dtstart=s0))
    for prop, add in (("rdate", rs.rdate), ("exdate", rs.exdate)):
        vals = master.get(prop)
        vals = vals if isinstance(vals, list) else ([vals] if vals is not None else [])
        for lst in vals:
            for d in getattr(lst, "dts", []):
                add(conv(d.dt))
    rs.rdate(s0)
    lo = datetime.combine(win_start, dtime())
    hi = datetime.combine(win_end, dtime())
    if not naive:
        lo, hi = lo.replace(tzinfo=NY), hi.replace(tzinfo=NY)
    out = []
    for occ in rs.xafter(lo, inc=True):
        if occ >= hi:
            break
        out.append(occ.date() if allday else occ)
        if len(out) >= MAX_EXPAND:
            break
    return out


def object_items(href: str, etag: str | None, ics: str, win_start: date, win_end: date) -> list[Item]:
    """Every X16-mappable item in one calendar object (singles always; occurrences within the window)."""
    from icalendar import Calendar
    try:
        cal = Calendar.from_ical(ics)
    except Exception as e:  # noqa: BLE001
        log.warning("unparseable calendar object %s: %s", href, type(e).__name__)
        return []
    evs = list(cal.walk("VEVENT"))
    masters = [e for e in evs if e.get("recurrence-id") is None]
    overrides = {_rid_str(e.decoded("recurrence-id")): e for e in evs if e.get("recurrence-id") is not None}
    out: list[Item] = []
    for m in masters:
        uid = str(m.get("uid", ""))
        if not uid or m.get("dtstart") is None:
            continue
        if m.get("rrule") is None and m.get("rdate") is None:
            if not _cancelled(m):
                d, tm = _to_ny(m.decoded("dtstart"))
                out.append(Item(uid, "", href, etag, d, tm, str(m.get("summary", ""))))
            continue
        try:
            occs = _expand(m, win_start, win_end)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot expand recurrence of %s: %s", uid, e)
            occs = []
        for occ in occs:
            rid = _rid_str(occ)
            ev = overrides.pop(rid, None) or m
            if _cancelled(ev):
                continue
            start = occ if ev is m else ev.decoded("dtstart")
            d, tm = _to_ny(start)
            out.append(Item(uid, rid, href, etag, d, tm, str(ev.get("summary", "")), True))
        for rid, ev in overrides.items():   # moved into the window from outside, or RDATE-less override
            if _cancelled(ev):
                continue
            d, tm = _to_ny(ev.decoded("dtstart"))
            if win_start <= d < win_end and uid == str(ev.get("uid", "")):
                out.append(Item(uid, rid, href, etag, d, tm, str(ev.get("summary", "")), True))
    return out


# =========================================================================== state

SCHEMA = """
CREATE TABLE IF NOT EXISTS cal_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cal_objects (href TEXT PRIMARY KEY, uid TEXT, etag TEXT, ics TEXT);
CREATE INDEX IF NOT EXISTS cal_objects_uid ON cal_objects(uid);
CREATE TABLE IF NOT EXISTS cal_slot_ver (
    slot INTEGER NOT NULL, rev INTEGER NOT NULL,
    raw BLOB NOT NULL,                 -- the 32-byte record
    kind TEXT NOT NULL,                -- 'empty' | 'event' | 'local' (X16 record we cannot sync)
    origin TEXT NOT NULL,              -- 'hub' | 'x16'
    uid TEXT, rid TEXT, href TEXT,
    etag TEXT,                         -- server etag when this version was synced
    shash TEXT,                        -- hash of (date, time, full summary) when synced
    summary TEXT,                      -- full server summary when synced
    PRIMARY KEY (slot, rev));
CREATE TABLE IF NOT EXISTS cal_conflicts (
    id INTEGER PRIMARY KEY, at INTEGER, slot INTEGER, uid TEXT, rid TEXT,
    kind TEXT, field TEXT, x16_value TEXT, server_value TEXT, kept TEXT, acked INTEGER DEFAULT 0);
"""


@dataclass
class Ver:
    slot: int
    rev: int
    raw: bytes
    kind: str = "empty"
    origin: str = "hub"
    uid: str | None = None
    rid: str | None = None
    href: str | None = None
    etag: str | None = None
    shash: str | None = None
    summary: str | None = None

    @property
    def rec(self) -> X16Rec:
        return X16Rec.from_bytes(self.raw)

    @property
    def key(self):
        return (self.uid, self.rid or "")


@dataclass
class PullResult:
    counter: int
    records: list[tuple[int, bytes]]
    full: bool                        # True: the X16 must replace ALL slots (records has all 128)
    conflicts: list[dict] = field(default_factory=list)
    unplaced: int = 0                 # server events that don't fit on the X16 right now


class MassDelete(HubError):
    def __init__(self, n: int, msg: str):
        super().__init__(msg)
        self.n = n


@dataclass
class PushResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: int = 0
    unsyncable: int = 0


# =========================================================================== engine

class CalSync:
    """One instance per sync session (or long-lived). Not thread-safe; uses a file lock across processes."""

    def __init__(self, oc, conn: sqlite3.Connection | None = None, *, cal_name: str = DEFAULT_CAL,
                 charset: str = "petscii", today: Callable[[], date] | None = None,
                 lock_path: Path | None = None):
        if charset not in CHARSETS:
            raise HubError(f"unknown charset {charset}")
        self.oc = oc
        self.cs = CHARSETS[charset]
        self.cal_name = cal_name
        self.db = conn or store_connect()
        self.db.isolation_level = None
        self.db.executescript(SCHEMA)
        self.today = today or (lambda: datetime.now(NY).date())
        self.lock_path = lock_path or (data_dir() / "calsync.lock")
        self._cal: dict | None = None
        self._items_cache: dict | None = None
        self.stats = PushResult()

    # ---------------------------------------------------------------- plumbing
    @contextmanager
    def _session(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._items_cache = None
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._bind_calendar()
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        finally:
            os.close(fd)

    def _meta(self, k: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT v FROM cal_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def _set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT INTO cal_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def _bind_calendar(self) -> None:
        if self._cal is None:
            cal, _ = self.oc.ensure_calendar(self.cal_name)
            self._cal = cal
        bound = self._meta("cal_href")
        if bound is None:
            self._set_meta("cal_href", self._cal["href"])
            self._set_meta("charset", self.cs.name)
            self._set_meta("hub_id", secrets.token_hex(8))
            self._set_meta("counter", 0)
        elif bound != self._cal["href"]:
            raise HubError(f"hub.db is bound to calendar {bound}; run `calsync reset` to switch to {self._cal['href']}")
        elif self._meta("charset") != self.cs.name:
            raise HubError(f"hub.db was synced with charset {self._meta('charset')}, not {self.cs.name}")

    def _next_rev(self) -> int:
        c = int(self._meta("counter", "0")) + 1
        self._set_meta("counter", c)
        return c

    def counter(self) -> int:
        return int(self._meta("counter", "0"))

    def _versions(self, slot: int) -> list[Ver]:
        rows = self.db.execute("SELECT slot,rev,raw,kind,origin,uid,rid,href,etag,shash,summary FROM cal_slot_ver "
                               "WHERE slot=? ORDER BY rev", (slot,)).fetchall()
        vs = [Ver(*r[:2], bytes(r[2]), *r[3:]) for r in rows]
        return vs or [Ver(slot, 0, EMPTY)]

    def _current(self) -> dict[int, Ver]:
        rows = self.db.execute(
            "SELECT v.slot,v.rev,v.raw,v.kind,v.origin,v.uid,v.rid,v.href,v.etag,v.shash,v.summary FROM cal_slot_ver v "
            "JOIN (SELECT slot, MAX(rev) m FROM cal_slot_ver GROUP BY slot) x ON v.slot=x.slot AND v.rev=x.m").fetchall()
        cur = {s: Ver(s, 0, EMPTY) for s in range(NSLOTS)}
        for r in rows:
            cur[r[0]] = Ver(*r[:2], bytes(r[2]), *r[3:])
        return cur

    def _add_version(self, v: Ver) -> Ver:
        v.rev = self._next_rev()
        self.db.execute("INSERT INTO cal_slot_ver VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (v.slot, v.rev, v.raw, v.kind, v.origin, v.uid, v.rid, v.href, v.etag, v.shash, v.summary))
        return v

    def _conflict(self, slot, uid, rid, kind, fld, x16v, srvv, kept) -> None:
        self.db.execute("INSERT INTO cal_conflicts(at,slot,uid,rid,kind,field,x16_value,server_value,kept) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (int(datetime.now(timezone.utc).timestamp()), slot, uid, rid, kind, fld, x16v, srvv, kept))
        self.stats.conflicts += 1
        log.info("conflict slot=%s uid=%s %s/%s kept=%s", slot, uid, kind, fld, kept)

    def _has_conflict(self, uid: str | None, rid: str | None) -> bool:
        return self.db.execute("SELECT 1 FROM cal_conflicts WHERE uid IS ? AND COALESCE(rid,'')=? AND acked=0",
                               (uid, rid or "")).fetchone() is not None

    # ---------------------------------------------------------------- server mirror
    def pull_server(self) -> int:
        """Bring cal_objects up to date via sync-collection. Returns number of changed objects."""
        from .backend.owncloud import NotFound, OCError
        token = self._meta("sync_token")
        try:
            res = self.oc.sync_collection(self._cal, token)
            full = token is None
        except OCError as e:
            if token is None or e.status not in (400, 403, 409, 412, 507):
                raise
            log.warning("sync token rejected (%s); doing a full resync", e.status)
            res, full = self.oc.sync_collection(self._cal, None), True
        mirror = {h: e for h, e in self.db.execute("SELECT href, etag FROM cal_objects")}
        seen, n = set(), 0
        for ch in res["changed"]:
            href = unquote(ch["href"])
            if not href.endswith(".ics"):
                continue
            seen.add(href)
            if ch.get("etag") and mirror.get(href) == ch["etag"]:
                continue
            try:
                ev = self.oc.get_event(href)
            except NotFound:
                self._drop_object(href)
                continue
            self._store_object(href, ev["etag"] or ch.get("etag"), ev["ics"])
            n += 1
        gone = {unquote(h) for h in res["deleted"]}
        if full:
            gone |= set(mirror) - seen
        for href in gone:
            self._drop_object(href)
            n += 1
        if res.get("token"):
            self._set_meta("sync_token", res["token"])
        return n

    def _store_object(self, href: str, etag: str | None, ics) -> None:
        self._items_cache = None
        if isinstance(ics, bytes):
            ics = ics.decode("utf-8")
        m = re.search(r"^UID[;:](?:[^:\r\n]*:)?(.*?)\r?$", re.sub(r"\r?\n[ \t]", "", ics), re.M)
        uid = m.group(1).strip() if m else None
        self.db.execute("INSERT INTO cal_objects VALUES (?,?,?,?) ON CONFLICT(href) DO UPDATE SET "
                        "uid=excluded.uid, etag=excluded.etag, ics=excluded.ics", (href, uid, etag, ics))

    def _drop_object(self, href: str) -> None:
        self._items_cache = None
        self.db.execute("DELETE FROM cal_objects WHERE href=?", (href,))

    def _refresh_object(self, href: str) -> tuple[str | None, str | None]:
        from .backend.owncloud import NotFound
        try:
            ev = self.oc.get_event(href)
        except NotFound:
            self._drop_object(href)
            return None, None
        self._store_object(href, ev["etag"], ev["ics"])
        return ev["etag"], ev["ics"] if isinstance(ev["ics"], str) else ev["ics"].decode("utf-8")

    def _object(self, uid: str) -> tuple[str, str | None, str] | None:
        r = self.db.execute("SELECT href, etag, ics FROM cal_objects WHERE uid=?", (uid,)).fetchone()
        return tuple(r) if r else None

    def _window(self) -> tuple[date, date]:
        t = self.today()
        return t - timedelta(days=WINDOW_PAST), t + timedelta(days=WINDOW_FUTURE)

    def _items(self) -> dict[tuple[str, str], Item]:
        if self._items_cache is not None:
            return self._items_cache
        ws, we = self._window()
        out = {}
        for href, etag, ics in self.db.execute("SELECT href, etag, ics FROM cal_objects"):
            for it in object_items(href, etag, ics, ws, we):
                if YEAR_MIN <= it.date.year <= YEAR_MAX:
                    out.setdefault(it.key, it)
        self._items_cache = out
        return out

    def _lookup(self, uid: str, rid: str) -> Item | None:
        o = self._object(uid)
        if not o:
            return None
        href, etag, ics = o
        if rid:
            d = datetime.strptime(rid[:8], "%Y%m%d").date()
            ws, we = d - timedelta(days=2), d + timedelta(days=3)
        else:
            ws, we = self._window()
        return next((i for i in object_items(href, etag, ics, ws, we) if i.key == (uid, rid)), None)

    # ---------------------------------------------------------------- rendering
    def _render(self, it: Item, slot: int) -> bytes:
        text, _ = render_text(self.cs, it.tm, it.summary)
        fl = F_USED | (F_CONFLICT if self._has_conflict(it.uid, it.rid) else 0)
        return X16Rec.make(it.date, text, fl).to_bytes()

    def _ver_for(self, slot: int, it: Item, raw: bytes, origin: str) -> Ver:
        return Ver(slot, 0, raw, "event", origin, it.uid, it.rid, it.href, it.etag, it.shash(), clean_summary(it.summary))

    # ---------------------------------------------------------------- X16 -> server
    def _base(self, vers: list[Ver], since: int) -> Ver:
        known = [v for v in vers if v.rev <= since or v.origin == "x16"]
        return known[-1] if known else Ver(vers[0].slot, 0, EMPTY)

    def _process_push(self, slot: int, raw: bytes, since: int) -> None:
        rec = X16Rec.from_bytes(raw)
        vers = self._versions(slot)
        cur, base = vers[-1], self._base(vers, since)
        if rec.norm() == cur.rec.norm() or rec.norm() == base.rec.norm() or \
                any(rec.norm() == v.rec.norm() for v in vers if v.rev > since):
            return                                   # X16 has some version we know: no local change
        if not rec.used:
            if since == 0:
                return          # full/unknown sync: an empty slot is "X16 doesn't have it", never a delete
            self._x16_delete(slot, base)
            self._add_version(Ver(slot, 0, EMPTY, "empty", "x16"))
        elif not base.rec.used or base.kind != "event":
            self._x16_create(slot, rec, base)
        elif rec.flags & F_EDITED:
            self._x16_edit(slot, base, rec)
        else:                                        # the app can't edit: a changed slot is a new event
            self._x16_delete(slot, base)
            self._x16_create(slot, rec, base)

    def _x16_delete(self, slot: int, base: Ver) -> None:
        from .backend.owncloud import NotFound, PreconditionFailed
        if base.kind != "event" or not base.uid:
            return
        it = self._lookup(base.uid, base.rid or "")
        if it is None:
            return                                   # already gone on the server
        changed = (it.etag != base.etag) if not base.rid else (it.shash() != base.shash)
        if changed:
            self._conflict(slot, base.uid, base.rid, "x16-delete-vs-server-edit", "event",
                           "(deleted on X16)", clean_summary(it.summary), "server")
            return
        if not base.rid:
            try:
                self.oc.delete_event(it.href, it.etag)
            except NotFound:
                pass
            except PreconditionFailed:
                self._refresh_object(it.href)
                self._conflict(slot, base.uid, "", "x16-delete-vs-server-edit", "event",
                               "(deleted on X16)", clean_summary(it.summary), "server")
                return
            self._drop_object(it.href)
            self.stats.deleted += 1
        else:
            self._modify_object(base.uid, lambda cal: _exdate(cal, base.rid))
            self.stats.deleted += 1

    def _x16_create(self, slot: int, rec: X16Rec, base: Ver) -> None:
        d = rec.date
        if d is None or not YEAR_MIN <= d.year <= YEAR_MAX or not rec.text.strip():
            self.stats.unsyncable += 1
            self._add_version(Ver(slot, 0, rec.to_bytes(), "local", "x16"))
            log.warning("slot %d: X16 record has an invalid date/empty text; kept on the X16, not synced", slot)
            return
        tm, body = split_text(rec.text)
        summary = self.cs.decode(body).strip() or self.cs.decode(rec.text)
        # adopt an identical, unmapped server item (hub.db lost / re-initial sync) instead of duplicating
        mapped = {v.key for v in self._current().values() if v.kind == "event"}
        want = rec.norm()
        for it in self._items().values():
            if it.key not in mapped and X16Rec.from_bytes(self._render(it, slot)).norm() == want:
                self._add_version(self._ver_for(slot, it, rec.to_bytes(), "x16"))
                return
        hub_id = self._meta("hub_id")
        uid = "x16-" + hashlib.sha256(f"{hub_id}:{slot}:{base.rev}:{rec.to_bytes().hex()}".encode()).hexdigest()[:24]
        ics = build_new_event(uid, d, tm, summary)
        from .backend.owncloud import PreconditionFailed
        try:
            href, _ = self.oc.create_event(self._cal, ics, uid)
        except PreconditionFailed:                   # retry after an interrupted session: it exists already
            href = self._cal["href"].rstrip("/") + "/" + uid + ".ics"
        etag, _ = self._refresh_object(href)
        if etag is None:
            raise HubError(f"created {href} but cannot read it back")
        self.stats.created += 1
        it = self._lookup(uid, "")
        self._add_version(self._ver_for(slot, it, rec.to_bytes(), "x16") if it else
                          Ver(slot, 0, rec.to_bytes(), "event", "x16", uid, "", href, etag))

    def _x16_edit(self, slot: int, base: Ver, rec: X16Rec) -> None:
        it = self._lookup(base.uid, base.rid or "")
        if it is None:                               # edit vs. server delete: recreate, never lose the edit
            self._conflict(slot, base.uid, base.rid, "x16-edit-vs-server-delete", "event",
                           self.cs.decode(rec.text), "(deleted in ownCloud)", "x16")
            self._x16_create(slot, X16Rec(rec.flags & ~F_EDITED, rec.year, rec.month, rec.day, rec.text), base)
            return
        b = base.rec
        b_tm, b_vis = split_text(b.text)
        x_tm, x_vis = split_text(rec.text)
        if x_vis != b_vis and len(rec.text) == TXTMAX and b_vis.startswith(x_vis):
            x_vis = b_vis                             # only cut off by a longer time prefix: not a text edit
        s_vis = render_text(self.cs, it.tm, it.summary)[0][len(b"00:00 ") if it.tm else 0:]
        fields = {
            "date": (b.date, rec.date, it.date),
            "time": (b_tm, x_tm, it.tm),
            "text": (b_vis, x_vis, s_vis if clean_summary(it.summary) != (base.summary or "") else b_vis),
        }
        new = {}
        notes = []
        for f, (bv, xv, sv) in fields.items():
            if xv == bv:
                continue                              # X16 didn't touch it -> server value stays
            if sv != bv and sv != xv:                 # both changed differently -> LWW (X16 arrived last)
                srv_txt = clean_summary(it.summary) if f == "text" else str(sv)
                x_txt = self.cs.decode(xv) if f == "text" else str(xv)
                self._conflict(slot, base.uid, base.rid, "both-edited", f, x_txt, srv_txt, "x16")
                notes.append(f"{f}: ownCloud had {srv_txt!r}")
            new[f] = xv
        if not new:
            return
        if "text" in new:
            if clean_summary(it.summary) == (base.summary or ""):
                new["text"] = merge_visible(self.cs, it.summary, b_vis, x_vis)
            else:
                new["text"] = self.cs.decode(x_vis)
        if rec.date is None:
            new.pop("date", None)
        stamp = datetime.now(NY).strftime("%Y-%m-%d %H:%M")
        note = f"[X16 sync conflict {stamp}: " + "; ".join(notes) + "]" if notes else None
        self._modify_object(base.uid, lambda cal: _apply_edit(cal, base.rid or "", new, note))
        self.stats.updated += 1

    def _modify_object(self, uid: str, fn) -> None:
        """GET-modify-PUT with If-Match; retries on 412 against the fresh copy."""
        from icalendar import Calendar
        from .backend.owncloud import PreconditionFailed
        for _ in range(3):
            o = self._object(uid)
            if not o:
                return
            href, etag, ics = o
            cal = Calendar.from_ical(ics)
            if not fn(cal):
                return
            try:
                cal.add_missing_timezones()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.oc.update_event(href, cal.to_ical(), etag)
            except PreconditionFailed:
                self._refresh_object(href)
                continue
            self._refresh_object(href)
            return
        raise HubError(f"event {uid} keeps changing on the server; try again")

    # ---------------------------------------------------------------- server -> X16 view
    def _rebuild_view(self) -> int:
        items = self._items()
        cur = self._current()
        today = self.today()
        per_day: dict[date, int] = {}
        mapped: set = set()
        for slot, v in cur.items():
            if v.kind == "event":
                it = items.get(v.key)
                if it is None or v.key in mapped:
                    self._add_version(Ver(slot, 0, EMPTY, "empty", "hub"))
                    cur[slot] = Ver(slot, 0, EMPTY)
                    continue
                mapped.add(v.key)
                raw = self._render(it, slot)
                if raw != v.raw:
                    cur[slot] = self._add_version(self._ver_for(slot, it, raw, "hub"))
                elif (it.etag, it.shash()) != (v.etag, v.shash):   # invisible change: rebase in place
                    self.db.execute("UPDATE cal_slot_ver SET etag=?, shash=?, summary=?, href=? WHERE slot=? AND rev=?",
                                    (it.etag, it.shash(), clean_summary(it.summary), it.href, slot, v.rev))
            if cur[slot].rec.used and cur[slot].rec.date:
                d = cur[slot].rec.date
                per_day[d] = per_day.get(d, 0) + 1

        def prio(it: Item):
            dd = (it.date - today).days
            return (0, dd) if dd >= 0 else (1, -dd), it.tm or (-1, -1), it.summary

        pending = sorted((it for k, it in items.items() if k not in mapped), key=prio)
        free = [s for s in range(NSLOTS) if not cur[s].rec.used]
        horizon = today - timedelta(days=WINDOW_PAST)
        evictable = sorted((s for s, v in cur.items() if v.kind == "event" and v.rec.date and v.rec.date < horizon),
                           key=lambda s: cur[s].rec.date, reverse=True)   # pop() takes the oldest
        unplaced = 0
        for it in pending:
            if per_day.get(it.date, 0) >= MAXDAY:
                unplaced += 1
                continue
            if not free and evictable and it.date >= horizon:
                s = evictable.pop()
                per_day[cur[s].rec.date] -= 1
                self._add_version(Ver(s, 0, EMPTY, "empty", "hub"))
                free.append(s)
            if not free:
                unplaced += 1
                continue
            s = free.pop(0)
            cur[s] = self._add_version(self._ver_for(s, it, self._render(it, s), "hub"))
            per_day[it.date] = per_day.get(it.date, 0) + 1
        self._set_meta("unplaced", unplaced)
        return unplaced

    def _prune(self, since: int) -> None:
        for slot in range(NSLOTS):
            vers = self._versions(slot)
            base = self._base(vers, since)
            if base.rev:
                self.db.execute("DELETE FROM cal_slot_ver WHERE slot=? AND rev<?", (slot, base.rev))

    # ---------------------------------------------------------------- public interface (W5 wire layer)
    def apply_x16_changes(self, records: Iterable[tuple[int, bytes]], since: int) -> PushResult:
        """X16 -> hub. records = (slot, 32-byte record). Sending unchanged records is harmless (no-op)."""
        recs = self._validate(records)
        self.stats = PushResult()
        with self._session():
            since = self._clamp(since)
            self.pull_server()
            self._mass_delete_guard(recs, since)
            for slot, raw in recs:
                self._process_push(slot, raw, since)
            self._rebuild_view()
            self._prune(since)
        return self.stats

    def x16_changes_since(self, counter: int) -> PullResult:
        """Hub -> X16. Slots changed after `counter` (all 128 if counter is 0 or unknown)."""
        with self._session():
            counter = self._clamp(counter)
            self.pull_server()
            unplaced = self._rebuild_view()
            return self._pull_result(counter, unplaced)

    def sync(self, records: Iterable[tuple[int, bytes]], since: int) -> tuple[PushResult, PullResult]:
        """Push and pull in one session (what the Sync button does)."""
        recs = self._validate(records)
        self.stats = PushResult()
        with self._session():
            since = self._clamp(since)
            self.pull_server()
            self._mass_delete_guard(recs, since)
            for slot, raw in recs:
                self._process_push(slot, raw, since)
            unplaced = self._rebuild_view()
            self._prune(since)
            return self.stats, self._pull_result(since, unplaced)

    MASS_DELETE_MIN = 10

    def _mass_delete_guard(self, recs, since: int) -> None:
        """A lost/unreadable CALENDAR.DAT looks like 'the user deleted everything'. Refuse a push that
        would empty more than half of the mapped slots (and at least MASS_DELETE_MIN), unless
        `calsync allow-mass-delete` armed a one-shot override."""
        if since == 0:
            return
        cur = self._current()
        mapped = [s for s, v in cur.items() if v.rec.used and v.kind == "event"]
        incoming = dict(recs)
        gone = [s for s in mapped if s in incoming and not X16Rec.from_bytes(incoming[s]).used]
        if len(gone) >= self.MASS_DELETE_MIN and len(gone) * 2 > len(mapped):
            if self._meta("allow_mass_delete") == "1":
                self._set_meta("allow_mass_delete", "0")
                log.warning("mass delete of %d events allowed by override", len(gone))
                return
            raise MassDelete(len(gone), f"refusing to delete {len(gone)} of {len(mapped)} events in one sync "
                           "(lost CALENDAR.DAT?); if intended: python -m x16hub.calsync allow-mass-delete")

    def allow_mass_delete(self) -> None:
        with self._session():
            self._set_meta("allow_mass_delete", "1")

    def _pull_result(self, counter: int, unplaced: int) -> PullResult:
        cur = self._current()
        full = counter == 0
        recs = [(s, v.raw) for s, v in sorted(cur.items()) if full or v.rev > counter]
        return PullResult(self.counter(), recs, full, self._conflict_rows(), unplaced)

    def conflicts(self, include_acked: bool = False) -> list[dict]:
        with self._session():
            return self._conflict_rows(include_acked)

    def _conflict_rows(self, include_acked: bool = False) -> list[dict]:
        q = "SELECT id,at,slot,uid,rid,kind,field,x16_value,server_value,kept,acked FROM cal_conflicts"
        cols = ["id", "at", "slot", "uid", "rid", "kind", "field", "x16_value", "server_value", "kept", "acked"]
        rows = self.db.execute(q + ("" if include_acked else " WHERE acked=0") + " ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def ack_conflicts(self, slots: Iterable[int] | None = None) -> int:
        """The user has seen the conflicts (all, or for some slots). Clears the conflict bit on the next pull."""
        with self._session():
            if slots is None:
                n = self.db.execute("UPDATE cal_conflicts SET acked=1 WHERE acked=0").rowcount
            else:
                cur, n = self._current(), 0
                for s in slots:
                    v = cur.get(int(s))
                    n += self.db.execute("UPDATE cal_conflicts SET acked=1 WHERE acked=0 AND (slot=? OR "
                                         "(uid IS ? AND COALESCE(rid,'')=?))",
                                         (int(s), v.uid if v else None, (v.rid or "") if v else "")).rowcount
            self._rebuild_view()
            return n

    def status(self) -> dict:
        with self._session():
            cur = self._current()
            return {"calendar": self._cal["href"], "counter": self.counter(), "charset": self.cs.name,
                    "sync_token": self._meta("sync_token"),
                    "x16_used": sum(1 for v in cur.values() if v.rec.used),
                    "server_objects": self.db.execute("SELECT COUNT(*) FROM cal_objects").fetchone()[0],
                    "unplaced": int(self._meta("unplaced", "0")),
                    "open_conflicts": self.db.execute("SELECT COUNT(*) FROM cal_conflicts WHERE acked=0").fetchone()[0]}

    def _clamp(self, since: int) -> int:
        since = int(since)
        if since < 0 or since > self.counter():
            log.warning("X16 counter %s unknown to this hub (max %s): full resync", since, self.counter())
            return 0
        return since

    @staticmethod
    def _validate(records) -> list[tuple[int, bytes]]:
        out, seen = [], set()
        for slot, raw in records:
            slot = int(slot)
            if not 0 <= slot < NSLOTS or slot in seen:
                raise HubError(f"bad or duplicate slot {slot}")
            raw = bytes(raw)
            if len(raw) != RECSZ:
                raise HubError(f"slot {slot}: record must be {RECSZ} bytes")
            seen.add(slot)
            out.append((slot, raw))
        return out


# =========================================================================== ICS editing

def build_new_event(uid: str, d: date, tm: tuple[int, int] | None, summary: str) -> bytes:
    from icalendar import Calendar, Event
    cal = Calendar()
    cal.add("prodid", "-//X16 Office//x16hub calsync//EN")
    cal.add("version", "2.0")
    ev = Event()
    now = datetime.now(timezone.utc)
    ev.add("uid", uid)
    ev.add("summary", summary)
    if tm:
        st = datetime.combine(d, dtime(*tm), tzinfo=NY)
        ev.add("dtstart", st)
        ev.add("dtend", st + timedelta(hours=1))
    else:
        ev.add("dtstart", d)
        ev.add("dtend", d + timedelta(days=1))
    for p in ("dtstamp", "created", "last-modified"):
        ev.add(p, now)
    ev.add("x-x16-origin", "x16 calendar")
    cal.add_component(ev)
    cal.add_missing_timezones()
    return cal.to_ical()


def _touch(ev) -> None:
    now = datetime.now(timezone.utc)
    for p in ("dtstamp", "last-modified"):
        if p in ev:
            del ev[p]
        ev.add(p, now)
    seq = int(ev.get("sequence", 0)) + 1
    if "sequence" in ev:
        del ev["sequence"]
    ev.add("sequence", seq)


def _master(cal):
    return next((e for e in cal.walk("VEVENT") if e.get("recurrence-id") is None), None)


def _exdate(cal, rid: str) -> bool:
    m = _master(cal)
    if m is None:
        return False
    for comp in list(cal.subcomponents):
        if comp.name == "VEVENT" and comp.get("recurrence-id") is not None \
                and _rid_str(comp.decoded("recurrence-id")) == rid:
            cal.subcomponents.remove(comp)
    val = _rid_value(rid, m.decoded("dtstart"))
    ex = m.get("exdate")
    ex = ex if isinstance(ex, list) else ([ex] if ex is not None else [])
    if any(_rid_str(d.dt) == rid for lst in ex for d in getattr(lst, "dts", [])):
        return True   # still PUT: the override may have been removed
    m.add("exdate", val)
    _touch(m)
    return True


def _apply_edit(cal, rid: str, new: dict, note: str | None) -> bool:
    from icalendar import Event
    m = _master(cal)
    if m is None:
        return False
    ev = m
    if rid:
        ev = next((e for e in cal.walk("VEVENT") if e.get("recurrence-id") is not None
                   and _rid_str(e.decoded("recurrence-id")) == rid), None)
        if ev is None:                                 # new override = copy of master for that occurrence
            ev = Event.from_ical(m.to_ical())
            for p in ("rrule", "rdate", "exdate", "recurrence-id"):
                while p in ev:
                    del ev[p]
            ms = m.decoded("dtstart")
            rv = _rid_value(rid, ms)
            dur = (m.decoded("dtend") - ms) if m.get("dtend") is not None else None
            del ev["dtstart"]
            ev.add("dtstart", rv)
            if dur is not None:
                del ev["dtend"]
                ev.add("dtend", rv + dur)
            ev.add("recurrence-id", rv)
            cal.add_component(ev)
    if "text" in new:
        if "summary" in ev:
            del ev["summary"]
        ev.add("summary", new["text"])
    if "date" in new or "time" in new:
        _move(ev, new.get("date"), new["time"] if "time" in new else ...)
    if note:
        desc = str(ev.get("description", ""))
        if "description" in ev:
            del ev["description"]
        ev.add("description", (desc + "\n" if desc else "") + note)
        ev.add("x-x16-conflict", note)
    _touch(ev)
    return True


def _move(ev, new_date: date | None, new_tm) -> None:
    """Move DTSTART to new_date/new_tm (... = keep time), keeping the duration where it makes sense."""
    old = ev.decoded("dtstart")
    old_d, old_tm = _to_ny(old)
    d = new_date or old_d
    tm = old_tm if new_tm is ... else new_tm
    has_end = ev.get("dtend") is not None
    dur = (ev.decoded("dtend") - old) if has_end else None
    was_allday = not isinstance(old, datetime)
    if tm is None:
        start = d
        if not was_allday or dur is None:
            dur = timedelta(days=1)
    else:
        start = datetime.combine(d, dtime(*tm), tzinfo=NY)
        if was_allday or dur is None:
            dur = timedelta(hours=1)
    del ev["dtstart"]
    ev.add("dtstart", start)
    if has_end or ev.get("duration") is None:
        if has_end:
            del ev["dtend"]
        ev.add("dtend", start + dur)
    elif was_allday != (tm is None):
        del ev["duration"]
        ev.add("dtend", start + dur)


# =========================================================================== file mode / CLI

def sync_dat_file(cs: CalSync, dat: Path) -> tuple[PushResult, PullResult]:
    """Act as the X16: push CALENDAR.DAT, apply the pull, write it back (plus counter in CALSYNC.DAT).

    Same order the X16 app must use: write CALENDAR.DAT first, then the counter file.
    """
    dat = Path(dat)
    side = dat.with_name("CALSYNC.DAT" if dat.name.isupper() else "calsync.dat")
    hdr, recs = read_dat(dat.read_bytes()) if dat.exists() else (DEFAULT_HEADER, [EMPTY] * NSLOTS)
    since = int.from_bytes(side.read_bytes()[:4], "little") if side.exists() and side.stat().st_size >= 4 else 0
    push, pull = cs.sync(list(enumerate(recs)), since)
    for s, raw in pull.records:
        recs[s] = raw
    tmp = dat.with_suffix(".tmp")
    tmp.write_bytes(write_dat(hdr, recs))
    os.replace(tmp, dat)
    side.write_bytes(pull.counter.to_bytes(4, "little"))
    return push, pull


def _client():
    from .backend import owncloud as oc
    return oc.OwnCloud(oc.load_config())


def main(argv=None) -> int:
    os.umask(0o077)
    from .backend.owncloud import install_log_redaction
    logging.basicConfig(level=os.environ.get("X16HUB_LOG", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    p = argparse.ArgumentParser(prog="python -m x16hub.calsync", description="X16 calendar <-> ownCloud CalDAV sync")
    p.add_argument("--calendar", default=os.environ.get("X16HUB_CALENDAR", DEFAULT_CAL))
    p.add_argument("--charset", default="petscii", choices=sorted(CHARSETS))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("conflicts")
    sub.add_parser("ack")
    f = sub.add_parser("dat", help="sync a CALENDAR.DAT file directly (emulator fsroot / SD card image dir)")
    f.add_argument("path")
    sub.add_parser("reset", help="forget all calendar sync state in hub.db (server data untouched)")
    sub.add_parser("allow-mass-delete", help="let the next sync delete many events at once (one shot)")
    f.add_argument("--force", action="store_true", help="even if an X16 is bound to this calendar state")
    a = p.parse_args(argv)
    try:
        if a.cmd == "reset":
            db = store_connect()
            for t in ("cal_meta", "cal_objects", "cal_slot_ver", "cal_conflicts"):
                db.execute(f"DROP TABLE IF EXISTS {t}")
            db.commit()
            print("calendar sync state cleared; next X16 sync must be a full one (counter 0)")
            return 0
        if a.cmd == "dat" and not a.force:
            db = store_connect()
            try:
                row = db.execute("SELECT v FROM cal_meta WHERE k='x16_device'").fetchone()
            except sqlite3.Error:
                row = None
            finally:
                db.close()
            if row:
                print(f"error: hub calendar state belongs to X16 {row[0]!r}; syncing another CALENDAR.DAT "
                      "would mix slot maps (use X16HUB_DATA=<other dir>, or --force)", file=sys.stderr)
                return 1
        cs = CalSync(_client(), cal_name=a.calendar, charset=a.charset)
        if a.cmd == "status":
            for k, v in cs.status().items():
                print(f"{k:<15} {v}")
        elif a.cmd == "conflicts":
            for c in cs.conflicts():
                print(f"#{c['id']} slot {c['slot']} {c['kind']} {c['field']}: X16={c['x16_value']!r} "
                      f"ownCloud={c['server_value']!r} kept={c['kept']}")
        elif a.cmd == "allow-mass-delete":
            cs.allow_mass_delete()
            print("the next sync may delete many events at once")
        elif a.cmd == "ack":
            print(f"acknowledged {cs.ack_conflicts()} conflict(s)")
        elif a.cmd == "dat":
            push, pull = sync_dat_file(cs, Path(a.path))
            print(f"pushed: created={push.created} updated={push.updated} deleted={push.deleted} "
                  f"conflicts={push.conflicts} unsyncable={push.unsyncable}")
            print(f"pulled: {len(pull.records)} record(s), counter={pull.counter}, unplaced={pull.unplaced}, "
                  f"open conflicts={len(pull.conflicts)}")
        return 0
    except HubError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
