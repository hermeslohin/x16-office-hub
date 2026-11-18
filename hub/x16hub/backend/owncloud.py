"""ownCloud Classic 11 backend: WebDAV (docs) + CalDAV (calendar).

Security rules (project rule: nothing ships with a security bug):
  * creds come only from secrets/owncloud.json (or X16HUB_OC_CREDS), which must
    be 0600 and owned by us, in a non-group/world-accessible directory;
  * TLS is always verified against the configured ca_file -- there is no
    switch to turn verification off;
  * the app password never appears in repr(), log records or exception text
    (OCError scrubs it; a logging filter scrubs it too, as a second layer);
  * HTTP redirects are not followed (auth must never be replayed elsewhere).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit

import requests
from defusedxml import ElementTree as ET

from ..store import HubError, SECRETS_DIR, check_private_file

log = logging.getLogger("x16hub.owncloud")

DEFAULT_CREDS = SECRETS_DIR / "owncloud.json"
DOCS_FOLDER = "X16"
CAL_NAME = "x16"
TIMEOUT = (10, 60)

NS = {
    "d": "DAV:",
    "cal": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "oc": "http://owncloud.org/ns",
    "s": "http://sabredav.org/ns",
}
D = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CS = "{http://calendarserver.org/ns/}"


# --------------------------------------------------------------------------- errors / redaction

_SECRETS: set[str] = set()  # every app password loaded in this process


def redact(text: str) -> str:
    """Remove any loaded secret (and Basic-auth headers) from text."""
    if not isinstance(text, str):
        text = str(text)
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(?i)(authorization\s*[:=]\s*)(basic|bearer)\s+\S+", r"\1\2 ***", text)


class OCError(HubError):
    """Backend error. The message is always passed through redact()."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(redact(msg))
        self.status = status


class PreconditionFailed(OCError):
    """412: the etag (If-Match / If-None-Match) did not match -> edit conflict."""


class TooLarge(OCError):
    """Response body bigger than the caller's limit (read was aborted)."""


class NotFound(OCError):
    pass


class RedactFilter(logging.Filter):
    """Scrub secrets from every record that reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg, record.args = redact(msg), ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    lg = logger or logging.getLogger()
    for h in lg.handlers:
        if not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(RedactFilter())
    if not any(isinstance(f, RedactFilter) for f in lg.filters):
        lg.addFilter(RedactFilter())


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class OCConfig:
    base_url: str
    username: str
    app_password: str = field(repr=False)
    ca_file: str

    def __repr__(self) -> str:  # belt and braces: never show the password
        return (f"OCConfig(base_url={self.base_url!r}, username={self.username!r}, "
                f"app_password='***', ca_file={self.ca_file!r})")

    __str__ = __repr__


def creds_path() -> Path:
    return Path(os.environ.get("X16HUB_OC_CREDS") or DEFAULT_CREDS).expanduser()


def load_config(path: Path | None = None) -> OCConfig:
    p = Path(path) if path else creds_path()
    if not p.exists():
        raise HubError(f"{p} not found (see hub/README.md)")
    check_private_file(p)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HubError(f"{p} is not valid JSON") from None  # don't chain: text may hold the secret
    missing = [k for k in ("base_url", "username", "app_password", "ca_file") if not raw.get(k)]
    if missing:
        raise HubError(f"{p} is missing: {', '.join(missing)}")
    _SECRETS.add(str(raw["app_password"]))
    base = str(raw["base_url"]).rstrip("/")
    u = urlsplit(base)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise HubError("base_url must be a plain https://host[/path] URL")
    user = str(raw["username"])
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", user):
        raise HubError("username has unexpected characters")
    ca = Path(str(raw["ca_file"])).expanduser()
    if not ca.is_file():
        raise HubError(f"ca_file {ca} not found")
    return OCConfig(base, user, str(raw["app_password"]), str(ca))


# --------------------------------------------------------------------------- helpers

def join_path(*parts: str) -> str:
    """Join and percent-encode relative DAV path segments, refusing traversal."""
    segs: list[str] = []
    for part in parts:
        for s in str(part).split("/"):
            if s in ("", "."):
                continue
            if s == "..":
                raise OCError("path traversal ('..') is not allowed")
            segs.append(quote(s, safe=""))
    return "/".join(segs)


def norm_etag(etag: str | None) -> str | None:
    """ownCloud returns etags quoted; keep them quoted for If-Match, strip W/."""
    if not etag:
        return None
    e = etag.strip()
    if e.startswith("W/"):
        e = e[2:]
    if not (e.startswith('"') and e.endswith('"')):
        e = f'"{e}"'
    return e


def next_mtime(last_modified: str | None, now: float | None = None) -> int:
    """Unix mtime for the next write: strictly after the previous one (HTTP-date)."""
    import time
    from email.utils import parsedate_to_datetime
    t = int(now if now is not None else time.time())
    if last_modified:
        try:
            t = max(t, int(parsedate_to_datetime(last_modified).timestamp()) + 1)
        except (TypeError, ValueError):
            pass
    return t


MAX_BODY = 16 << 20          # cap on any response body read into memory


def _int(s: str | None) -> int:
    try:
        return max(0, int((s or "0").strip()))
    except ValueError:
        return 0


def parse_multistatus(xml: bytes | str) -> list[dict]:
    """Parse a 207 multistatus into [{'href', 'status', 'props': {clark-name: Element}}].

    Only props from 2xx propstats are returned. defusedxml blocks XXE/billion-laughs.
    """
    try:
        root = ET.fromstring(xml)
    except Exception as e:  # noqa: BLE001 - ParseError, EntitiesForbidden, DTDForbidden ...
        raise OCError(f"unparseable multistatus from server ({type(e).__name__})") from None
    out = []
    for resp in root.findall(f"{D}response"):
        href = unquote(resp.findtext(f"{D}href", default=""))
        props: dict = {}
        for ps in resp.findall(f"{D}propstat"):
            st = ps.findtext(f"{D}status", default="")
            if " 2" not in st:
                continue
            prop = ps.find(f"{D}prop")
            if prop is not None:
                for el in prop:
                    props[el.tag] = el
        out.append({"href": href, "status": resp.findtext(f"{D}status", default=""), "props": props})
    return out


def _text(props: dict, tag: str) -> str | None:
    el = props.get(tag)
    return el.text if el is not None and el.text is not None else None


def build_event(uid: str, summary: str, start, end, description: str | None = None) -> bytes:
    """VCALENDAR with one VEVENT; aware datetimes keep their zone, VTIMEZONE added."""
    from icalendar import Calendar, Event
    cal = Calendar()
    cal.add("prodid", "-//X16 Office//x16hub//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    ev.add("dtstart", start)
    ev.add("dtend", end)
    from datetime import datetime, timezone
    ev.add("dtstamp", datetime.now(timezone.utc))
    if description:
        ev.add("description", description)
    cal.add_component(ev)
    cal.add_missing_timezones()
    return cal.to_ical()


def parse_event(ics: bytes | str) -> dict:
    from icalendar import Calendar
    cal = Calendar.from_ical(ics)
    for ev in cal.walk("VEVENT"):
        return {
            "uid": str(ev.get("uid")),
            "summary": str(ev.get("summary", "")),
            "start": ev.decoded("dtstart"),
            "end": ev.decoded("dtend") if ev.get("dtend") else None,
            "sequence": int(ev.get("sequence", 0)),
        }
    raise OCError("no VEVENT in calendar object")


# --------------------------------------------------------------------------- client

class OwnCloud:
    def __init__(self, cfg: OCConfig, session: requests.Session | None = None):
        self.cfg = cfg
        _SECRETS.add(cfg.app_password)
        self.s = session or requests.Session()
        self.s.auth = (cfg.username, cfg.app_password)
        self.s.verify = cfg.ca_file           # always verify TLS against our CA
        self.s.trust_env = False              # no proxy/netrc/REQUESTS_CA_BUNDLE overrides
        self.s.max_redirects = 0
        self.s.headers["User-Agent"] = "x16hub/0.2"
        root = urlsplit(cfg.base_url)
        self._origin = f"{root.scheme}://{root.netloc}"
        self.files_root = f"{cfg.base_url}/remote.php/dav/files/{quote(cfg.username)}/"
        self.cal_root = f"{cfg.base_url}/remote.php/dav/calendars/{quote(cfg.username)}/"

    def __repr__(self) -> str:
        return f"OwnCloud({self.cfg!r})"

    # ---- transport
    def _req(self, method: str, url: str, *, ok: Iterable[int], headers=None, data=None,
             max_bytes: int = MAX_BODY) -> requests.Response:
        if not url.startswith(self.cfg.base_url + "/"):
            raise OCError("refusing request outside base_url")
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            r = self.s.request(method, url, headers=headers or {}, data=data,
                               timeout=TIMEOUT, allow_redirects=False, stream=True)
            try:
                cl = int(r.headers.get("Content-Length") or 0)
            except ValueError:
                cl = 0
            if cl > max_bytes:
                r.close()
                raise TooLarge(f"{method} {url}: response larger than {max_bytes} bytes", r.status_code)
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    r.close()
                    raise TooLarge(f"{method} {url}: response larger than {max_bytes} bytes", r.status_code)
            r._content = bytes(buf)                  # bounded body; r.content / r.text work as usual
            r._content_consumed = True
        except requests.exceptions.SSLError:
            raise OCError(f"TLS verification failed for {self._origin} (ca_file={self.cfg.ca_file})") from None
        except requests.RequestException as e:
            raise OCError(f"{method} {url}: {type(e).__name__}: {e}") from None
        if r.status_code in ok:
            return r
        msg = f"{method} {url} -> HTTP {r.status_code}"
        m = re.search(r"<s:message>(.*?)</s:message>", r.text or "", re.S)
        if m:
            msg += f": {m.group(1).strip()[:200]}"
        if r.status_code == 412:
            raise PreconditionFailed(msg, 412)
        if r.status_code == 404:
            raise NotFound(msg, 404)
        raise OCError(msg, r.status_code)

    def _abs(self, href: str) -> str:
        return href if href.startswith("http") else self._origin + quote(href, safe="/@:~!$&'()*+,;=-._")

    # ---- WebDAV
    PROPFIND_FILES = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:prop><d:getetag/><d:getlastmodified/><d:getcontentlength/><d:resourcetype/>"
        "<oc:fileid/></d:prop></d:propfind>"
    )

    def file_url(self, path: str = "") -> str:
        return self.files_root + join_path(path)

    def list(self, path: str = "", depth: int = 1) -> list[dict]:
        r = self._req("PROPFIND", self.file_url(path) + ("/" if path else ""), ok=(207,),
                      headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
                      data=self.PROPFIND_FILES)
        base = unquote(urlsplit(self.files_root).path)
        out = []
        for it in parse_multistatus(r.content):
            p = it["props"]
            rt = p.get(f"{D}resourcetype")
            out.append({
                "path": it["href"][len(base):] if it["href"].startswith(base) else it["href"],
                "etag": norm_etag(_text(p, f"{D}getetag")),
                "last_modified": _text(p, f"{D}getlastmodified"),
                "size": _int(_text(p, f"{D}getcontentlength")),
                "is_dir": rt is not None and rt.find(f"{D}collection") is not None,
            })
        return out

    def stat(self, path: str) -> dict | None:
        try:
            return self.list(path, depth=0)[0]
        except NotFound:
            return None

    def get(self, path: str, max_bytes: int = MAX_BODY) -> tuple[bytes, str | None]:
        r = self._req("GET", self.file_url(path), ok=(200,), max_bytes=max_bytes)
        return r.content, norm_etag(r.headers.get("ETag") or r.headers.get("OC-ETag"))

    def put(self, path: str, data: bytes, *, if_match: str | None = None,
            create_only: bool = False) -> str | None:
        """Upload. if_match -> 412 PreconditionFailed if changed since; create_only -> 412 if exists.

        ownCloud 11 derives a file's etag from its whole-second mtime: two writes
        in the same second keep the SAME etag, which would silently defeat If-Match
        conflict detection. So on overwrite we pin X-OC-MTime to max(now, old+1),
        guaranteeing every write gets a new mtime and therefore a new etag.
        """
        h = {"Content-Type": "application/octet-stream"}
        if if_match:
            h["If-Match"] = norm_etag(if_match)
        if create_only:
            h["If-None-Match"] = "*"
        else:
            cur = self.stat(path)
            h["X-OC-MTime"] = str(next_mtime(cur["last_modified"] if cur else None))
        r = self._req("PUT", self.file_url(path), ok=(200, 201, 204), headers=h, data=data)
        new = norm_etag(r.headers.get("ETag") or r.headers.get("OC-ETag"))
        if if_match and new == norm_etag(if_match):
            raise OCError(f"PUT {path}: server kept the old etag; conflict detection would be unsafe")
        return new

    def mkcol(self, path: str) -> None:
        self._req("MKCOL", self.file_url(path) + "/", ok=(201,))

    def delete(self, path: str, *, if_match: str | None = None) -> None:
        if not join_path(path):
            raise OCError("refusing to DELETE the files root")
        h = {"If-Match": norm_etag(if_match)} if if_match else {}
        self._req("DELETE", self.file_url(path), ok=(204,), headers=h)

    def _transfer(self, method: str, src: str, dst: str, overwrite: bool, is_dir: bool) -> None:
        """WebDAV MOVE/COPY inside the user's files. Overwrite F -> 412 PreconditionFailed if dst exists."""
        if not join_path(src) or not join_path(dst):
            raise OCError(f"{method}: empty source or destination")
        tail = "/" if is_dir else ""
        self._req(method, self.file_url(src) + tail, ok=(201, 204),
                  headers={"Destination": self.file_url(dst) + tail, "Overwrite": "T" if overwrite else "F",
                           "Depth": "infinity"})

    def move(self, src: str, dst: str, *, overwrite: bool = False, is_dir: bool = False) -> None:
        self._transfer("MOVE", src, dst, overwrite, is_dir)

    def copy(self, src: str, dst: str, *, overwrite: bool = False, is_dir: bool = False) -> None:
        self._transfer("COPY", src, dst, overwrite, is_dir)

    def ensure_folder(self, path: str = DOCS_FOLDER) -> bool:
        """Create the folder if missing. Returns True if it was created."""
        st = self.stat(path)
        if st is not None:
            if not st["is_dir"]:
                raise OCError(f"{path} exists and is not a folder")
            return False
        try:
            self.mkcol(path)
        except OCError as e:
            if e.status == 405:  # raced: already exists
                return False
            raise
        return True

    # ---- CalDAV
    PROPFIND_CALS = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/" '
        'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:prop><d:resourcetype/><d:displayname/>'
        "<cs:getctag/><d:sync-token/><cal:supported-calendar-component-set/>"
        "<d:supported-report-set/></d:prop></d:propfind>"
    )

    def calendars(self) -> list[dict]:
        r = self._req("PROPFIND", self.cal_root, ok=(207,),
                      headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                      data=self.PROPFIND_CALS)
        out = []
        for it in parse_multistatus(r.content):
            p = it["props"]
            rt = p.get(f"{D}resourcetype")
            if rt is None or rt.find(f"{CAL}calendar") is None:
                continue
            comps = p.get(f"{CAL}supported-calendar-component-set")
            reports = p.get(f"{D}supported-report-set")
            rep_names = set()
            if reports is not None:
                for rep in reports.iter(f"{D}report"):
                    for child in rep:
                        rep_names.add(child.tag)
            out.append({
                "href": it["href"],
                "name": it["href"].rstrip("/").rsplit("/", 1)[-1],
                "displayname": _text(p, f"{D}displayname"),
                "ctag": _text(p, f"{CS}getctag"),
                "sync_token": _text(p, f"{D}sync-token"),
                "components": [c.get("name") for c in comps] if comps is not None else [],
                "reports": sorted(t.split("}")[-1] for t in rep_names),
            })
        return out

    def calendar(self, name: str = CAL_NAME) -> dict | None:
        return next((c for c in self.calendars() if c["name"] == name), None)

    def ensure_calendar(self, name: str = CAL_NAME, displayname: str = "X16") -> tuple[dict, bool]:
        c = self.calendar(name)
        if c:
            return c, False
        body = (
            '<?xml version="1.0" encoding="utf-8"?><cal:mkcalendar xmlns:d="DAV:" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            f"<d:displayname>{_xml_escape(displayname)}</d:displayname>"
            "<cal:supported-calendar-component-set><cal:comp name=\"VEVENT\"/>"
            "</cal:supported-calendar-component-set></d:prop></d:set></cal:mkcalendar>"
        )
        self._req("MKCALENDAR", self.cal_root + join_path(name) + "/", ok=(201,),
                  headers={"Content-Type": "application/xml; charset=utf-8"}, data=body)
        c = self.calendar(name)
        if not c:
            raise OCError(f"calendar {name} not visible after MKCALENDAR")
        return c, True

    def ctag(self, cal: dict) -> str | None:
        r = self._req("PROPFIND", self._abs(cal["href"]), ok=(207,),
                      headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                      data='<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
                           "<d:prop><cs:getctag/><d:sync-token/></d:prop></d:propfind>")
        items = parse_multistatus(r.content)
        return _text(items[0]["props"], f"{CS}getctag") if items else None

    def events(self, cal: dict, start, end) -> list[dict]:
        """calendar-query REPORT for VEVENTs overlapping [start, end) (aware datetimes)."""
        fmt = lambda t: t.astimezone(_utc()).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
        body = (
            '<?xml version="1.0" encoding="utf-8"?><cal:calendar-query xmlns:d="DAV:" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><cal:calendar-data/>'
            '</d:prop><cal:filter><cal:comp-filter name="VCALENDAR"><cal:comp-filter name="VEVENT">'
            f'<cal:time-range start="{fmt(start)}" end="{fmt(end)}"/>'
            "</cal:comp-filter></cal:comp-filter></cal:filter></cal:calendar-query>"
        )
        r = self._req("REPORT", self._abs(cal["href"]), ok=(207,),
                      headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, data=body)
        out = []
        for it in parse_multistatus(r.content):
            data = _text(it["props"], f"{CAL}calendar-data")
            if data is None:
                continue
            ev = parse_event(data)
            ev.update(href=it["href"], etag=norm_etag(_text(it["props"], f"{D}getetag")), ics=data)
            out.append(ev)
        return out

    def get_event(self, href: str) -> dict:
        r = self._req("GET", self._abs(href), ok=(200,))
        ev = parse_event(r.content)
        ev.update(href=href, etag=norm_etag(r.headers.get("ETag")), ics=r.text)
        return ev

    def create_event(self, cal: dict, ics: bytes, uid: str | None = None) -> tuple[str, str | None]:
        uid = uid or str(uuid.uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", uid):
            raise OCError("uid has characters unsafe for a resource name")
        href = cal["href"].rstrip("/") + "/" + uid + ".ics"
        r = self._req("PUT", self._abs(href), ok=(201, 204),
                      headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}, data=ics)
        return href, norm_etag(r.headers.get("ETag"))

    def update_event(self, href: str, ics: bytes, etag: str) -> str | None:
        r = self._req("PUT", self._abs(href), ok=(200, 201, 204),
                      headers={"Content-Type": "text/calendar; charset=utf-8", "If-Match": norm_etag(etag)},
                      data=ics)
        return norm_etag(r.headers.get("ETag"))

    def delete_event(self, href: str, etag: str | None = None) -> None:
        h = {"If-Match": norm_etag(etag)} if etag else {}
        self._req("DELETE", self._abs(href), ok=(204,), headers=h)

    def sync_collection(self, cal: dict, token: str | None) -> dict:
        """RFC 6578. token=None -> initial sync. Returns {token, changed:[{href,etag}], deleted:[href]}."""
        tok = f"<d:sync-token>{_xml_escape(token)}</d:sync-token>" if token else "<d:sync-token/>"
        body = ('<?xml version="1.0" encoding="utf-8"?><d:sync-collection xmlns:d="DAV:">'
                f"{tok}<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>")
        r = self._req("REPORT", self._abs(cal["href"]), ok=(207,),
                      headers={"Content-Type": "application/xml; charset=utf-8"}, data=body)
        root = ET.fromstring(r.content)
        changed, deleted = [], []
        for it in parse_multistatus(r.content):
            if "404" in it["status"]:
                deleted.append(it["href"])
            elif it["props"]:
                changed.append({"href": it["href"], "etag": norm_etag(_text(it["props"], f"{D}getetag"))})
        return {"token": root.findtext(f"{D}sync-token"), "changed": changed, "deleted": deleted}


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _utc():
    from datetime import timezone
    return timezone.utc
